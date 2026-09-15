//
//  DevLog.swift —— 一行代码接入的日志 SDK
//
//  怎么用（三步）
//    1. 把这个文件拖进 Xcode 工程（勾上 target）
//    2. 在 AppDelegate 或 @main 里加一行：
//         DevLog.start(host: "192.168.0.104", port: 8918, tag: "Driver")
//    3. 想记日志的地方：
//         DevLog.i("下单页加载完成")
//         DevLog.e("提交失败", extra: ["code": 500, "url": "/waybill/create"])
//
//  然后打开工作台的「代码日志」，日志会实时流过来，还能让 AI 直接分析。
//
//  设计上的几个取舍
//    · **不阻塞主线程**：写日志只是塞进内存队列，发送在后台线程做
//    · **不拖慢 App**：批量发送（攒够 30 条或每 1.5 秒发一次）
//    · **断网不丢日志**：发不出去就留在队列里，下次一起发（队列有上限，防内存涨）
//    · **不带第三方依赖**：只用 Foundation
//    · **release 包里自动关掉**：默认只在 DEBUG 生效，不用怕上线泄露日志
//
//  如果不想让它只在 DEBUG 生效，start 时传 `alsoInRelease: true`
//

import Foundation

#if canImport(UIKit)
import UIKit
#endif

public final class DevLog {

    // ── 配置 ──
    public struct Config {
        public var host: String
        public var port: Int
        public var tag: String          // 默认标签，比如模块名
        public var device: String       // 设备标识，默认取机型
        public var minLevel: Level      // 低于这个级别的不发
        public var batchSize: Int       // 攒够多少条发一次
        public var flushInterval: Double// 或者最多等这么久
        public var alsoInRelease: Bool  // release 包里也要吗
        public var capturePrint: Bool   // 顺便把 print() 也收上来
        public init(host: String, port: Int = 8918, tag: String = "App",
                    device: String = "", minLevel: Level = .debug,
                    batchSize: Int = 30, flushInterval: Double = 1.5,
                    alsoInRelease: Bool = false, capturePrint: Bool = false) {
            self.host = host; self.port = port; self.tag = tag
            self.device = device.isEmpty ? DevLog.modelName() : device
            self.minLevel = minLevel; self.batchSize = batchSize
            self.flushInterval = flushInterval
            self.alsoInRelease = alsoInRelease
            self.capturePrint = capturePrint
        }
    }

    public enum Level: String {
        case debug, info, warn, error, fatal
        var rank: Int {
            switch self {
            case .debug: return 0
            case .info:  return 1
            case .warn:  return 2
            case .error: return 3
            case .fatal: return 4
            }
        }
    }

    // ── 单例状态 ──
    private static var cfg: Config?
    private static let queue = DispatchQueue(label: "devlog.queue")
    private static var pending: [[String: Any]] = []
    private static var timer: DispatchSourceTimer?
    private static let maxPending = 2000          // 断网太久就别一直攒了
    private static var dropped = 0

    // ══════════════════════════════════════════
    // 启动
    // ══════════════════════════════════════════

    /// 开始上报。DEBUG 之外的构建默认不生效（见 alsoInRelease）
    @discardableResult
    public static func start(host: String, port: Int = 8918, tag: String = "App",
                             minLevel: Level = .debug, alsoInRelease: Bool = false,
                             capturePrint: Bool = false) -> Bool {
        #if !DEBUG
        if !alsoInRelease {
            print("[DevLog] release 构建，未启用（要启用就传 alsoInRelease: true）")
            return false
        }
        #endif
        let c = Config(host: host, port: port, tag: tag, minLevel: minLevel,
                       alsoInRelease: alsoInRelease, capturePrint: capturePrint)
        cfg = c
        startTimer()
        installCrashHandler()
        if capturePrint { swizzlePrint() }
        i("DevLog 已连接 \(host):\(port)", tag: "DevLog")
        return true
    }

    public static func stop() {
        queue.sync {
            timer?.cancel(); timer = nil
            cfg = nil
            pending.removeAll()
        }
    }

    private static func startTimer() {
        queue.async {
            timer?.cancel()
            let t = DispatchSource.makeTimerSource(queue: queue)
            t.schedule(deadline: .now() + (cfg?.flushInterval ?? 1.5),
                       repeating: cfg?.flushInterval ?? 1.5)
            t.setEventHandler { flushLocked() }
            t.resume()
            timer = t
        }
    }

    // ══════════════════════════════════════════
    // 记日志的入口
    // ══════════════════════════════════════════

    public static func d(_ msg: String, tag: String? = nil,
                         file: String = #file, line: Int = #line,
                         func fn: String = #function, extra: [String: Any]? = nil) {
        write(.debug, msg, tag, file, line, fn, extra)
    }
    public static func i(_ msg: String, tag: String? = nil,
                         file: String = #file, line: Int = #line,
                         func fn: String = #function, extra: [String: Any]? = nil) {
        write(.info, msg, tag, file, line, fn, extra)
    }
    public static func w(_ msg: String, tag: String? = nil,
                         file: String = #file, line: Int = #line,
                         func fn: String = #function, extra: [String: Any]? = nil) {
        write(.warn, msg, tag, file, line, fn, extra)
    }
    public static func e(_ msg: String, tag: String? = nil,
                         file: String = #file, line: Int = #line,
                         func fn: String = #function, extra: [String: Any]? = nil) {
        write(.error, msg, tag, file, line, fn, extra)
    }
    /// 严重错误：写完立刻发，不等批量
    public static func fatal(_ msg: String, tag: String? = nil,
                             file: String = #file, line: Int = #line,
                             func fn: String = #function, extra: [String: Any]? = nil) {
        write(.fatal, msg, tag, file, line, fn, extra, immediate: true)
    }

    /// 把一个 Error 直接记下来（网络错误、解析错误最常用）
    public static func error(_ err: Error, tag: String? = nil, context: String = "",
                             file: String = #file, line: Int = #line,
                             func fn: String = #function) {
        let ns = err as NSError
        write(.error, context.isEmpty ? "\(err)" : "\(context)：\(err)",
              tag, file, line, fn,
              ["domain": ns.domain, "code": ns.code,
               "desc": ns.localizedDescription], immediate: true)
    }

    /// 测一段代码耗时 —— 排查卡顿用
    @discardableResult
    public static func measure<T>(_ label: String, tag: String? = nil,
                                  _ block: () throws -> T) rethrows -> T {
        let t0 = CFAbsoluteTimeGetCurrent()
        defer {
            let ms = Int((CFAbsoluteTimeGetCurrent() - t0) * 1000)
            let lv: Level = ms > 500 ? .warn : .debug
            write(lv, "⏱ \(label) 耗时 \(ms)ms", tag, #file, #line, #function, nil)
        }
        return try block()
    }

    // ══════════════════════════════════════════
    // 内部
    // ══════════════════════════════════════════

    private static func write(_ level: Level, _ msg: String, _ tag: String?,
                              _ file: String, _ line: Int, _ fn: String,
                              _ extra: [String: Any]?, immediate: Bool = false) {
        guard let c = cfg, level.rank >= c.minLevel.rank else { return }
        var rec: [String: Any] = [
            "level": level.rawValue,
            "tag": tag ?? c.tag,
            "msg": msg,
            "file": (file as NSString).lastPathComponent,
            "line": line,
            "func": fn,
            "device": c.device,
            "app": Bundle.main.bundleIdentifier ?? "",
            "ts": timestamp(),
        ]
        if let e = extra { rec["extra"] = e.mapValues { "\($0)" } }

        queue.async {
            pending.append(rec)
            if pending.count > maxPending {
                let drop = pending.count - maxPending
                pending.removeFirst(drop)
                dropped += drop
            }
            if immediate || pending.count >= (cfg?.batchSize ?? 30) {
                flushLocked()
            }
        }
    }

    private static func timestamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f.string(from: Date())
    }

    /// 必须在 queue 上调用
    private static func flushLocked() {
        guard let c = cfg, !pending.isEmpty else { return }
        var batch = pending
        pending.removeAll()
        if dropped > 0 {
            batch.append(["level": "warn", "tag": "DevLog", "device": c.device,
                          "msg": "⚠️ 之前有 \(dropped) 条日志因为断网太久被丢弃",
                          "ts": timestamp()])
            dropped = 0
        }
        guard let url = URL(string: "http://\(c.host):\(c.port)/api/log") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 5
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "source": "sdk",
            "logs": batch,
        ])
        // 用 ephemeral 会话：不落 cookie、不缓存，免得影响 App 自己的网络
        let sess = URLSession(configuration: .ephemeral)
        sess.dataTask(with: req) { _, _, err in
            if err != nil {
                // 发失败就把这批放回去（放前面，保持时间顺序），下次再发
                queue.async {
                    pending.insert(contentsOf: batch, at: 0)
                    if pending.count > maxPending {
                        let drop = pending.count - maxPending
                        pending.removeFirst(drop)
                        dropped += drop
                    }
                }
            }
        }.resume()
    }

    // ── 崩溃也报上来（这是最有价值的一类日志）──
    private static func installCrashHandler() {
        NSSetUncaughtExceptionHandler { exception in
            let stack = exception.callStackSymbols.joined(separator: "\n")
            DevLog.fatal("💥 未捕获异常：\(exception.name.rawValue) — \(exception.reason ?? "")",
                         tag: "Crash", extra: ["stack": stack])
            // 崩溃时等一小会儿让日志发出去（不能等太久，系统会杀进程）
            Thread.sleep(forTimeInterval: 1.2)
        }
    }

    // ── 可选：把 print() 也收上来 ──
    private static func swizzlePrint() {
        // print 没法直接 swizzle，这里给个替代：
        // 建议直接用 DevLog.i()，或者把 print 换成 DevLog.i
        // 保留这个开关是为了以后接 stdout 重定向
    }

    public static func modelName() -> String {
        #if canImport(UIKit)
        var sys = utsname()
        uname(&sys)
        let m = Mirror(reflecting: sys.machine)
        let id = m.children.reduce("") { acc, el in
            guard let v = el.value as? Int8, v != 0 else { return acc }
            return acc + String(UnicodeScalar(UInt8(v)))
        }
        return id.isEmpty ? UIDevice.current.model : id
        #else
        return "Mac"
        #endif
    }

    /// 手动清一下待发队列（比如退到后台前）
    public static func flush() {
        queue.sync { flushLocked() }
    }
}

#if canImport(UIKit)
import UIKit
#endif
