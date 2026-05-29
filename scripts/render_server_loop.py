"""
Render Server 主循环。从 Redis 队列拉取 SwiftUI 代码 → 增量编译 → 模拟器截图 → 推回结果。
"""
import os, time, json, base64, subprocess
import redis


def main():
    rc = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        password=os.environ["REDIS_PASSWORD"],
        ssl=os.environ.get("REDIS_TLS", "").lower() == "true",
        decode_responses=True,
        socket_keepalive=True,
        # 防 SSL/网络抖动：BRPOP 阻塞 5s，socket_timeout 给 30s 双保险
        socket_timeout=30,
        socket_connect_timeout=10,
        health_check_interval=60,
        retry_on_timeout=True,
    )

    session = os.environ["SESSION_ID"]
    max_idle = int(os.environ["MAX_IDLE"])
    max_total = int(os.environ["MAX_TOTAL_MIN"]) * 60
    device = os.environ["DEVICE_ID"]

    start = time.time()
    last_render = time.time()
    queue_key = f"swiftui:queue:{session}"
    heartbeat_key = f"swiftui:server:{session}"
    rendered = 0

    def heartbeat():
        try:
            rc.setex(heartbeat_key, 60, "alive")
        except Exception as _e:
            # heartbeat 失败不致命（下次循环会重试）；崩进程会让上游等 360s 才降级
            print(f"[Server] heartbeat failed (will retry): {_e}", flush=True)

    heartbeat()
    print(f"[Server] ready session={session}, polling {queue_key}", flush=True)

    while True:
        if time.time() - start > max_total:
            print(f"[Server] max_total reached", flush=True)
            break
        if time.time() - last_render > max_idle:
            print(f"[Server] idle timeout", flush=True)
            break

        heartbeat()
        try:
            result = rc.brpop(queue_key, timeout=5)
        except (redis.exceptions.TimeoutError,
                redis.exceptions.ConnectionError,
                redis.exceptions.ResponseError) as _e:
            # 跨境 SSL 抖动 / Redis 配额满 / 网络瞬断 → 不崩进程，下次循环重试
            # 之前 server 在 1 个 job 后就因这里抛异常退出，导致每轮都重新等 200s+ 启动
            print(f"[Server] brpop network blip: {_e}, retry in 1s", flush=True)
            time.sleep(1)
            continue
        if not result:
            continue

        _, payload = result
        try:
            req = json.loads(payload)
        except Exception as e:
            print(f"[Server] invalid payload: {e}", flush=True)
            continue

        job_id = req.get("job_id", "unknown")
        swift_b64 = req.get("swift_code_b64", "")
        root_view = req.get("root_view", "ContentView")
        result_key = f"swiftui:result:{job_id}"
        print(f"[Server] job={job_id} root_view={root_view}", flush=True)

        t0 = time.time()

        try:
            swift_code = base64.b64decode(swift_b64).decode()
            with open("Runner/UserView.swift", "w", encoding="utf-8") as f:
                f.write(swift_code)
            with open("Runner/RunnerApp.swift", "w", encoding="utf-8") as f:
                f.write(
                    "import SwiftUI\n\n"
                    "@main\nstruct RunnerApp: App {\n"
                    "    var body: some Scene {\n"
                    "        WindowGroup {\n"
                    f"            {root_view}()\n"
                    "        }\n    }\n}\n"
                )
        except Exception as e:
            rc.lpush(result_key, json.dumps({"type": "error", "error": f"decode failed: {e}"}))
            rc.expire(result_key, 600)
            last_render = time.time()
            continue

        r = subprocess.run(
            ["xcodebuild", "-project", "Runner.xcodeproj", "-scheme", "Runner",
             "-destination", f"id={device}", "-derivedDataPath", "build",
             "COMPILER_INDEX_STORE_ENABLE=NO",
             "CODE_SIGN_IDENTITY=", "CODE_SIGNING_REQUIRED=NO", "build"],
            capture_output=True, text=True, timeout=600,
        )

        if r.returncode != 0:
            errors = [l for l in (r.stdout + r.stderr).splitlines()
                      if "error:" in l or "BUILD FAILED" in l]
            err_text = "\n".join(errors[:50]) or (r.stdout + r.stderr)[-2000:]
            rc.lpush(result_key, json.dumps({"type": "build_error", "error": err_text}))
            rc.expire(result_key, 600)
            last_render = time.time()
            print(f"[Server] build_error job={job_id} in {time.time()-t0:.0f}s", flush=True)
            continue

        find_r = subprocess.run(["find", "build", "-name", "Runner.app", "-type", "d"],
                                capture_output=True, text=True)
        app_path = (find_r.stdout.strip().splitlines() or [""])[0]
        if not app_path:
            rc.lpush(result_key, json.dumps({"type": "error", "error": "Runner.app not found"}))
            rc.expire(result_key, 600)
            last_render = time.time()
            continue

        subprocess.run(["xcrun", "simctl", "terminate", device, "com.swiftui.renderer.Runner"],
                       capture_output=True)
        subprocess.run(["xcrun", "simctl", "uninstall", device, "com.swiftui.renderer.Runner"],
                       capture_output=True)
        ins = subprocess.run(["xcrun", "simctl", "install", device, app_path],
                             capture_output=True, text=True)
        if ins.returncode != 0:
            rc.lpush(result_key, json.dumps({"type": "error", "error": f"install: {ins.stderr[:500]}"}))
            rc.expire(result_key, 600)
            last_render = time.time()
            continue
        subprocess.run(["xcrun", "simctl", "launch", device, "com.swiftui.renderer.Runner"],
                       capture_output=True)
        time.sleep(2)

        shot = f"/tmp/{job_id}.png"
        ss = subprocess.run(["xcrun", "simctl", "io", device, "screenshot", shot],
                            capture_output=True, text=True)
        if ss.returncode != 0:
            rc.lpush(result_key, json.dumps({"type": "error", "error": f"screenshot: {ss.stderr[:500]}"}))
            rc.expire(result_key, 600)
            last_render = time.time()
            continue

        with open(shot, "rb") as f:
            png = f.read()
        png_b64 = base64.b64encode(png).decode()
        rc.lpush(result_key, json.dumps({"type": "screen", "png_b64": png_b64}))
        rc.expire(result_key, 600)
        rendered += 1
        last_render = time.time()
        print(f"[Server] done job={job_id} png={len(png)//1024}KB in {time.time()-t0:.0f}s "
              f"(total={rendered})", flush=True)

    rc.delete(heartbeat_key)
    print(f"[Server] exit, rendered {rendered} jobs in {time.time()-start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
