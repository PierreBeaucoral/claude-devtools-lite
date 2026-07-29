// Claude DevTools — standalone macOS window for claude-devtools-lite.
// A native WKWebView wrapper: starts the Python server if needed, opens the
// dashboard with the auth cookie, and on quit shuts the server down gracefully
// (Claude Code sessions get to run their SessionEnd hooks) — but only if this
// app instance was the one that started the server.
import Cocoa
import WebKit

let PORT = ProcessInfo.processInfo.environment["PORT"] ?? "3456"
let BASE = "http://127.0.0.1:\(PORT)"

func serverDir() -> URL {
    let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
    let sibling = bundleParent.appendingPathComponent("claude-devtools-lite")
    if FileManager.default.fileExists(atPath: sibling.appendingPathComponent("server.py").path) {
        return sibling
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Desktop/claude-devtools-lite")
}

func serverUp() -> Bool {
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    var req = URLRequest(url: URL(string: BASE + "/")!, timeoutInterval: 1)
    req.httpMethod = "HEAD"
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        ok = (resp as? HTTPURLResponse) != nil
        sem.signal()
    }.resume()
    sem.wait()
    return ok
}

class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var startedServer = false
    var token = ""

    func applicationDidFinishLaunching(_ n: Notification) {
        let dir = serverDir()
        if !serverUp() {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", dir.appendingPathComponent("server.py").path,
                           "--port", PORT]
            let log = FileHandle(forWritingAtPath: "/dev/null")
            p.standardOutput = log; p.standardError = log
            try? p.run()
            startedServer = true
            for _ in 0..<40 { if serverUp() { break }; usleep(250_000) }
        }
        // token lives outside the source folder (never in the git repo);
        // fall back to the legacy in-repo path for older installs
        let appSupport = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/claude-devtools/token")
        token = ((try? String(contentsOf: appSupport, encoding: .utf8))
                 ?? (try? String(contentsOf: dir.appendingPathComponent(".token"),
                                 encoding: .utf8)) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.uiDelegate = self

        let screen = NSScreen.main?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(1500, screen.width * 0.92), h = min(950, screen.height * 0.92)
        window = NSWindow(
            contentRect: NSRect(x: screen.midX - w/2, y: screen.midY - h/2,
                                width: w, height: h),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Claude DevTools"
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        webView.load(URLRequest(url: URL(string: BASE + "/launch?k=\(token)")!))
    }

    // JS confirm() (used by the ⏻ button) needs a native handler in WKWebView
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.runModal(); completionHandler()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        guard startedServer, !token.isEmpty else { return .terminateNow }
        // graceful server shutdown: sessions get up to ~35s for their hooks
        var req = URLRequest(url: URL(string: BASE + "/api/shutdown")!,
                             timeoutInterval: 40)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token, forHTTPHeaderField: "X-Devtools-Token")
        req.httpBody = "{}".data(using: .utf8)
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }.resume()
        return .terminateLater
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate

// minimal main menu so Cmd+Q / Cmd+W / copy-paste work
let mainMenu = NSMenu()
let appItem = NSMenuItem(); mainMenu.addItem(appItem)
let appMenu = NSMenu()
appMenu.addItem(withTitle: "Quit Claude DevTools",
                action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
appItem.submenu = appMenu
let editItem = NSMenuItem(); mainMenu.addItem(editItem)
let editMenu = NSMenu(title: "Edit")
editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)),
                 keyEquivalent: "a")
editItem.submenu = editMenu
app.mainMenu = mainMenu

app.run()
