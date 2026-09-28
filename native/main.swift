// Claude DevTools — standalone macOS window for claude-devtools-lite.
// A native WKWebView wrapper: starts the Python server if needed, opens the
// dashboard with the auth cookie, and on quit shuts the server down gracefully
// (Claude Code sessions get to run their SessionEnd hooks) — but only if this
// app instance was the one that started the server.
import Cocoa
import CryptoKit
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

/// Does the server on PORT hold `token`? It answers /hello?n=<nonce> with
/// HMAC-SHA256(token, "cdl-hello:" + nonce), so we never hand the token to
/// some other program that happens to be listening on the port.
func serverHoldsToken(_ token: String) -> Bool {
    let nonce = (0..<16).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    guard let url = URL(string: BASE + "/hello?n=" + nonce) else { return false }
    var mac: String? = nil
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: URLRequest(url: url, timeoutInterval: 3)) { data, _, _ in
        if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            mac = o["mac"] as? String
        }
        sem.signal()
    }.resume()
    sem.wait()
    let want = HMAC<SHA256>.authenticationCode(
        for: Data(("cdl-hello:" + nonce).utf8), using: SymmetricKey(data: Data(token.utf8)))
        .map { String(format: "%02x", $0) }.joined()
    return mac == want
}

class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
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
            // startup crashes land here; the server also keeps its own
            // rotating log in Application Support
            let logURL = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs/claude-devtools.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil,
                                           attributes: [.posixPermissions: 0o600])
            let log = FileHandle(forWritingAtPath: logURL.path)
                ?? FileHandle(forWritingAtPath: "/dev/null")
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
        webView.navigationDelegate = self

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

        if !serverHoldsToken(token) {
            let a = NSAlert()
            a.messageText = "Another program is answering on port \(PORT)"
            a.informativeText = "It is not your claude-devtools server, so the app will not send it your access token. Quit whatever uses the port, or set PORT to another value."
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        webView.load(URLRequest(url: URL(string: BASE + "/launch?k=\(token)")!))
    }

    // target="_blank" links (transcript links, add-on sources): open them in
    // the default browser instead of silently doing nothing
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // the window only ever shows the dashboard: any other top-level navigation
    // goes to the browser; iframes (previews) are left alone
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard navigationAction.targetFrame?.isMainFrame ?? false,
              let url = navigationAction.request.url else { return decisionHandler(.allow) }
        if url.absoluteString.hasPrefix(BASE + "/") || url.absoluteString == BASE {
            return decisionHandler(.allow)
        }
        if ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
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
