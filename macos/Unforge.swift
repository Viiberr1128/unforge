import AppKit
import WebKit
import Darwin

/// Native chrome around the same local workspace used by the CLI and browser.
/// Native folder selection is the only JavaScript-to-native request.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var loading: NSStackView!
    var message: NSTextField!
    var retryButton: NSButton!
    var engine: Process?
    var parentPipe: Pipe?
    var engineLog: FileHandle?
    var engineID = ""
    var ready = false
    var stopping = false
    var quitPending = false
    var startupGeneration = 0
    var probeTimer: Timer?
    var healthTimer: Timer?
    var downloads: [ObjectIdentifier: (temporary: URL, destination: URL)] = [:]
    var home: URL
    let origin: URL
    let session: URLSession

    override init() {
        let env = ProcessInfo.processInfo.environment
        let path = (env["UNFORGE_HOME"] ?? UserDefaults.standard.string(forKey: "workspacePath") ?? "~/.local/share/unforge") as NSString
        home = URL(fileURLWithPath: path.expandingTildeInPath, isDirectory: true).standardizedFileURL.resolvingSymlinksInPath()
        let port = Int(env["UNFORGE_PORT"] ?? "4319") ?? 4319
        origin = URL(string: "http://127.0.0.1:\((1024...65535).contains(port) ? port : 4319)")!
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 2
        config.timeoutIntervalForResource = 3
        session = URLSession(configuration: config)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        umask(0o077)
        installMenus()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "unforgeFolders")
        web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.translatesAutoresizingMaskIntoConstraints = false
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Unforge"
        window.minSize = NSSize(width: 660, height: 520)
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("UnforgeWorkspace")
        window.center()
        let container = NSView()
        window.contentView = container
        container.addSubview(web)
        NSLayoutConstraint.activate([web.leadingAnchor.constraint(equalTo: container.leadingAnchor), web.trailingAnchor.constraint(equalTo: container.trailingAnchor), web.topAnchor.constraint(equalTo: container.topAnchor), web.bottomAnchor.constraint(equalTo: container.bottomAnchor)])
        let title = NSTextField(labelWithString: "Your software. On your Mac.")
        title.font = .systemFont(ofSize: 28, weight: .semibold)
        message = NSTextField(wrappingLabelWithString: "Opening your workspace…")
        message.alignment = .center
        message.font = .systemFont(ofSize: 15)
        retryButton = NSButton(title: "Try again", target: self, action: #selector(retry))
        retryButton.isHidden = true
        let folder = NSButton(title: "Show project folder", target: self, action: #selector(showFolder))
        loading = NSStackView(views: [title, message, retryButton, folder])
        loading.orientation = .vertical
        loading.spacing = 20
        loading.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(loading)
        NSLayoutConstraint.activate([loading.centerXAnchor.constraint(equalTo: container.centerXAnchor), loading.centerYAnchor.constraint(equalTo: container.centerYAnchor), loading.widthAnchor.constraint(lessThanOrEqualToConstant: 520), loading.leadingAnchor.constraint(greaterThanOrEqualTo: container.leadingAnchor, constant: 30)])
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startWorkspace()
    }

    func installMenus() {
        let main = NSMenu()
        NSApp.mainMenu = main
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem(); main.addItem(item)
            let menu = NSMenu(title: title); item.submenu = menu; return menu
        }
        func item(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String = "", target: AnyObject? = nil) {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.target = target; menu.addItem(i)
        }
        let app = submenu("Unforge")
        item(app, "About Unforge", #selector(about), target: self)
        app.addItem(.separator())
        item(app, "Hide Unforge", #selector(NSApplication.hide(_:)), "h")
        app.addItem(.separator())
        item(app, "Quit Unforge", #selector(NSApplication.terminate(_:)), "q")
        let file = submenu("File")
        item(file, "Open Workspace…", #selector(openWorkspace), target: self)
        item(file, "Show Project Folder", #selector(showFolder), "o", target: self)
        item(file, "Open in Browser", #selector(openBrowser), target: self)
        item(file, "Show Engine Log", #selector(showLog), target: self)
        file.addItem(.separator())
        item(file, "Close Window", #selector(NSWindow.performClose(_:)), "w")
        let edit = submenu("Edit")
        item(edit, "Undo", Selector(("undo:")), "z")
        item(edit, "Redo", Selector(("redo:")), "Z")
        edit.addItem(.separator())
        item(edit, "Cut", #selector(NSText.cut(_:)), "x")
        item(edit, "Copy", #selector(NSText.copy(_:)), "c")
        item(edit, "Paste", #selector(NSText.paste(_:)), "v")
        item(edit, "Select All", #selector(NSText.selectAll(_:)), "a")
        let view = submenu("View")
        item(view, "Reload Workspace", #selector(reload), "r", target: self)
        item(view, "Larger Text", #selector(zoomIn), "+", target: self)
        item(view, "Smaller Text", #selector(zoomOut), "-", target: self)
        item(view, "Actual Size", #selector(actualSize), "0", target: self)
        let windows = submenu("Window"); NSApp.windowsMenu = windows
        item(windows, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        item(windows, "Show Unforge", #selector(showWindow), target: self)
    }

    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Unforge", .applicationVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.3.1", .credits: NSAttributedString(string: "Built for humans. Operated by AI. Owned by you.\nLocal workspace · MIT open source\nYour projects stay outside the app bundle.")])
    }
    @objc func openWorkspace() {
        guard downloads.isEmpty else { return }
        confirmDiscard(action: "Switch workspace") { allowed in
            guard allowed else { return }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true; panel.canChooseFiles = false
            panel.allowsMultipleSelection = false; panel.prompt = "Open workspace"
            panel.message = "Choose an Unforge workspace or a recovered workspace. Your current files stay where they are. Running local apps will stop."
            panel.beginSheetModal(for: self.window) { response in
                guard response == .OK, let selected = panel.url else { return }
                let target = selected.standardizedFileURL.resolvingSymlinksInPath()
                guard target != self.home else { return }
                let fm = FileManager.default
                let children = (try? fm.contentsOfDirectory(at: target, includingPropertiesForKeys: nil)) ?? []
                let recognized = fm.fileExists(atPath: target.appendingPathComponent("UNFORGE-RECOVERY.json").path) || children.contains { fm.fileExists(atPath: $0.appendingPathComponent(".unforge/project.json").path) }
                guard recognized else {
                    let alert = NSAlert(); alert.messageText = "Choose an Unforge workspace"
                    alert.informativeText = "This folder does not contain recognizable Unforge projects or a recovery manifest. Import an ordinary app folder from Your projects instead."
                    alert.beginSheetModal(for: self.window); return
                }
                guard self.engine?.isRunning == true else {
                    self.status("This window is connected to an independently started engine. Stop that engine before switching workspaces.", retry: true); return
                }
                self.stopping = true
                self.healthTimer?.invalidate(); self.probeTimer?.invalidate()
                self.status("Closing the current workspace…")
                try? self.parentPipe?.fileHandleForWriting.close(); self.parentPipe = nil
                let process = self.engine
                DispatchQueue.global().async {
                    process?.waitUntilExit()
                    DispatchQueue.main.async {
                        self.engine = nil; self.home = target; self.stopping = false
                        UserDefaults.standard.set(target.path, forKey: "workspacePath")
                        self.startWorkspace()
                    }
                }
            }
        }
    }
    @objc func showFolder() { NSWorkspace.shared.open(home) }
    var logURL: URL { FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Unforge/engine.log") }
    @objc func showLog() { NSWorkspace.shared.selectFile(logURL.path, inFileViewerRootedAtPath: logURL.deletingLastPathComponent().path) }
    @objc func openBrowser() { if ready { NSWorkspace.shared.open(origin) } }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func zoomIn() { web.pageZoom = min(1.8, web.pageZoom + 0.1) }
    @objc func zoomOut() { web.pageZoom = max(0.7, web.pageZoom - 0.1) }
    @objc func actualSize() { web.pageZoom = 1 }
    @objc func reload() { confirmDiscard(action: "Reload", completion: { if $0 { self.startWorkspace() } }) }
    @objc func retry() { if engine?.isRunning != true { startWorkspace() } }

    func status(_ text: String, retry: Bool = false) {
        ready = false
        web.isHidden = true
        loading.isHidden = false
        message.stringValue = text
        retryButton.isHidden = !retry
    }

    func probe(_ completion: @escaping ([String: Any]?, Int?) -> Void) {
        session.dataTask(with: origin.appendingPathComponent("api/desktop")) { data, response, _ in
            let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            DispatchQueue.main.async { completion(object, (response as? HTTPURLResponse)?.statusCode) }
        }.resume()
    }
    func matches(_ data: [String: Any]) -> Bool {
        guard data["app"] as? String == "unforge", data["protocol"] as? Int == 1,
              let path = data["workspace"] as? String else { return false }
        return URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath() == home
    }
    func startWorkspace() {
        startupGeneration += 1
        let generation = startupGeneration
        status("Opening your workspace…")
        probe { data, code in
            guard generation == self.startupGeneration, !self.stopping else { return }
            if let data = data, self.matches(data) { self.showWorkspace(shared: self.engine?.isRunning != true); return }
            if code != nil {
                self.status("Port \(self.origin.port!) is already in use. Quit the older Unforge engine or the other app using this port, then try again. Your projects have not moved.", retry: true)
                return
            }
            self.launchEngine(generation: generation)
        }
    }
    func launchEngine(generation: Int) {
        guard let resources = Bundle.main.resourceURL else { return status("The app bundle is incomplete.") }
        let executable = resources.appendingPathComponent("engine/unforge-engine")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { return status("The bundled engine is missing. Reinstall Unforge; your projects are stored separately.") }
        // Do not run /usr/bin/git blindly: on a new Mac its stub opens an installer.
        let candidates = ["/opt/homebrew/bin/git", "/usr/local/bin/git", "/Library/Developer/CommandLineTools/usr/bin/git", "/Applications/Xcode.app/Contents/Developer/usr/bin/git"]
        guard let git = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            return status("Git is needed to keep your project history. Install Git or Apple's Command Line Tools, then reopen Unforge. Python and Node are already taken care of.", retry: true)
        }
        do {
            try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let log = logURL
            try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            if let size = try? log.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 5 * 1024 * 1024 { try FileManager.default.removeItem(at: log) }
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            engineLog = try FileHandle(forWritingTo: log); try engineLog?.seekToEnd()
            let process = Process(); engineID = UUID().uuidString
            process.executableURL = executable
            process.arguments = ["--home", home.path, "--port", String(origin.port!), "--desktop-parent", "--desktop-id", engineID]
            process.currentDirectoryURL = resources
            var env = ProcessInfo.processInfo.environment
            for key in Array(env.keys) where key.hasPrefix("PYTHON") || key.hasPrefix("GIT_") || key.hasPrefix("DYLD_") { env.removeValue(forKey: key) }
            var paths = [URL(fileURLWithPath: git).deletingLastPathComponent().path,
                         FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path,
                         "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            if let codexApp = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") {
                let tools = codexApp.appendingPathComponent("Contents/Resources")
                if FileManager.default.isExecutableFile(atPath: tools.appendingPathComponent("codex").path) { paths.append(tools.path) }
            }
            env["PATH"] = paths.joined(separator: ":")
            env["PYTHONUNBUFFERED"] = "1"
            process.environment = env
            let pipe = Pipe(); parentPipe = pipe
            process.standardInput = pipe
            process.standardOutput = engineLog
            process.standardError = engineLog
            process.terminationHandler = { process in DispatchQueue.main.async {
                guard !self.stopping, self.engine === process else { return }
                self.probeTimer?.invalidate(); self.healthTimer?.invalidate()
                self.engine = nil
                try? self.parentPipe?.fileHandleForWriting.close(); self.parentPipe = nil
                if self.ready {
                    self.window.subtitle = "Engine stopped — keep any unwritten text, then reload"
                } else {
                    self.status("The local engine stopped. Your saved projects remain on this Mac. Try again, or use File → Show Engine Log for details.", retry: true)
                }
            } }
            engine = process
            try process.run()
            try pipe.fileHandleForReading.close()
            let deadline = Date().addingTimeInterval(20)
            probeTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { timer in
                guard generation == self.startupGeneration, !self.stopping else { timer.invalidate(); return }
                self.probe { data, _ in
                    guard generation == self.startupGeneration, !self.stopping, !self.ready else { return }
                    if let data = data, self.matches(data), data["desktopId"] as? String == self.engineID {
                        timer.invalidate(); self.showWorkspace(shared: false)
                    } else if Date() > deadline {
                        timer.invalidate()
                        try? self.parentPipe?.fileHandleForWriting.close(); self.parentPipe = nil
                        self.status("The engine did not become ready. Try again after it stops, or use File → Show Engine Log.", retry: true)
                    }
                }
            }
        } catch {
            if engine?.isRunning == true { engine?.terminate() }
            engine = nil; parentPipe = nil
            status("Could not open the local engine: \(error.localizedDescription)", retry: true)
        }
    }
    func showWorkspace(shared: Bool) {
        ready = true
        window.subtitle = shared ? "Connected to your existing local engine" : "On this Mac"
        loading.isHidden = true
        web.isHidden = false
        web.load(URLRequest(url: origin))
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            // Health checks are attached to this window, and pause while hidden.
            guard self.window.isVisible, NSApp.isActive, self.ready else { return }
            self.probe { data, _ in
                if data == nil || !self.matches(data!) {
                    self.healthTimer?.invalidate()
                    // Preserve the loaded editor and drafts on an outage.
                    self.window.subtitle = "Engine unavailable — keep any unwritten text before reloading"
                }
            }
        }
    }
    func confirmDiscard(action: String, completion: @escaping (Bool) -> Void) {
        guard ready else { completion(true); return }
        func warn(_ detail: String) {
            self.showWindow()
            let alert = NSAlert()
            alert.messageText = "\(action) with unfinished work?"
            alert.informativeText = detail
            alert.addButton(withTitle: "Stay Here")
            alert.addButton(withTitle: action)
            alert.beginSheetModal(for: self.window) { completion($0 == .alertSecondButtonReturn) }
        }
        web.evaluateJavaScript("Boolean(window.unforgeDesktopHasDraft)") { value, error in
            if error != nil || value as? Bool == true {
                warn("There may be unwritten edits or an operation in progress. Stay here to finish or copy your work first.")
            } else if action == "Quit", self.engine?.isRunning == true {
                self.probe { data, _ in
                    if data == nil || (data?["pendingWork"] as? Int ?? 0) > 0 {
                        warn("Quitting stops running agent work. Your saved versions and durable proposals remain available when you reopen Unforge.")
                    } else { completion(true) }
                }
            } else { completion(true) }
        }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stopping { return .terminateNow }
        if quitPending { return .terminateCancel }
        quitPending = true
        confirmDiscard(action: "Quit") { allowed in
            guard allowed else { self.quitPending = false; NSApp.reply(toApplicationShouldTerminate: false); return }
            if !self.downloads.isEmpty {
                let alert = NSAlert(); alert.messageText = "Downloads are still in progress."
                alert.informativeText = "Finish or cancel the save dialogs before quitting."
                alert.beginSheetModal(for: self.window) { _ in self.quitPending = false; NSApp.reply(toApplicationShouldTerminate: false) }
                return
            }
            self.stopping = true
            self.healthTimer?.invalidate(); self.probeTimer?.invalidate()
            try? self.parentPipe?.fileHandleForWriting.close(); self.parentPipe = nil
            guard let process = self.engine, process.isRunning else { NSApp.reply(toApplicationShouldTerminate: true); return }
            // The pipe releases our own engine. An independently started CLI
            // server is deliberately left alone.
            DispatchQueue.global().async {
                process.waitUntilExit()
                DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
            }
        }
        return .terminateLater
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Red close preserves the web view and any drafts, matching normal Mac apps.
        sender.orderOut(nil); return false
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }

    func isLocal(_ url: URL) -> Bool { url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "unforgeFolders", message.frameInfo.isMainFrame,
              let url = message.frameInfo.request.url, isLocal(url),
              let body = message.body as? [String: String], body["action"] == "chooseFolder",
              let requestID = body["id"], UUID(uuidString: requestID) != nil else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false
        panel.prompt = "Choose folder"
        panel.beginSheetModal(for: window) { response in
            let result: [String: Any] = ["id": requestID, "path": response == .OK ? (panel.url?.path as Any? ?? NSNull()) : NSNull()]
            guard let data = try? JSONSerialization.data(withJSONObject: result), let json = String(data: data, encoding: .utf8) else { return }
            self.web.evaluateJavaScript("window.dispatchEvent(new CustomEvent('unforge-folder-picked', {detail: \(json)}))", completionHandler: nil)
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if navigationAction.targetFrame?.isMainFrame == false {
            decisionHandler(url.absoluteString == "about:srcdoc" || url.absoluteString == "about:blank" || isLocal(url) ? .allow : .cancel); return
        }
        if isLocal(url) {
            decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow); return
        }
        if url.scheme == "blob", url.absoluteString.hasPrefix("blob:" + origin.absoluteString + "/"), navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        if navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let response = navigationResponse.response as? HTTPURLResponse, response.value(forHTTPHeaderField: "Content-Disposition")?.lowercased().hasPrefix("attachment") == true { decisionHandler(.download) }
        else { decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download) }
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? ""), !isLocal(url) { NSWorkspace.shared.open(url) }
        return nil
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard frame.isMainFrame, let url = frame.request.url, isLocal(url) else { completionHandler(nil); return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "Continue"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = (suggestedFilename as NSString).lastPathComponent
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in
            guard result == .OK, let target = panel.url else { completionHandler(nil); return }
            do {
                let folder = FileManager.default.temporaryDirectory.appendingPathComponent("unforge-download-" + UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
                let temporary = folder.appendingPathComponent("download")
                self.downloads[ObjectIdentifier(download)] = (temporary, target)
                completionHandler(temporary)
            } catch { self.presentError(error); completionHandler(nil) }
        }
    }
    func downloadDidFinish(_ download: WKDownload) {
        guard let item = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: item.temporary.path)
            if FileManager.default.fileExists(atPath: item.destination.path) { _ = try FileManager.default.replaceItemAt(item.destination, withItemAt: item.temporary) }
            else { try FileManager.default.moveItem(at: item.temporary, to: item.destination) }
            try? FileManager.default.removeItem(at: item.temporary.deletingLastPathComponent())
        } catch { presentError(error) }
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let item = downloads.removeValue(forKey: ObjectIdentifier(download)) { try? FileManager.default.removeItem(at: item.temporary.deletingLastPathComponent()) }
        if (error as NSError).code != NSURLErrorCancelled { presentError(error) }
    }
    func presentError(_ error: Error) { let alert = NSAlert(error: error); alert.beginSheetModal(for: window) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
