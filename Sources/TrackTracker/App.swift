import AppKit
import WebKit

// TrackTracker.app: una finestra che mostra l'interfaccia di track_tracker.py.
// Lo script Python sta dentro l'app (Contents/Resources) e viene avviato in "modalità app":
// sceglie da solo una porta libera, scrive l'indirizzo su stdout e si chiude quando
// l'app chiude la sua stdin (anche se l'app va in crash).

@main
enum TrackTrackerApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private static let folderKey = "projectsFolder"
    private static let background = NSColor(red: 0x12 / 255, green: 0x12 / 255, blue: 0x14 / 255, alpha: 1)

    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var serverInput: Pipe?
    private var serverURL: URL?
    private var stderrText = ""

    private var folder: URL? {
        get {
            guard let path = UserDefaults.standard.string(forKey: Self.folderKey) else { return nil }
            var isDir: ObjCBool = false
            return FileManager.default.fileExists(atPath: path, isDirectory: &isDir) && isDir.boolValue
                ? URL(fileURLWithPath: path) : nil
        }
        set { UserDefaults.standard.set(newValue?.path, forKey: Self.folderKey) }
    }

    // MARK: avvio e chiusura

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let icon = NSImage(contentsOf: url), icon.isValid {
            NSApp.applicationIconImage = icon
        }
        installMenu()

        webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = self
        webView.underPageBackgroundColor = Self.background
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Track Tracker"
        window.minSize = NSSize(width: 820, height: 560)
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = Self.background
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("MainWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startLaunchCheckTimeout()

        if let folder {
            startServer(folder: folder)
        } else {
            showMessage("Benvenuto in Track Tracker",
                        "Scegli la cartella che contiene i tuoi progetti Ableton.",
                        button: "Scegli cartella…")
            DispatchQueue.main.async { self.chooseFolder(nil) }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // MARK: cartella dei progetti

    @objc func chooseFolder(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "Scegli"
        panel.message = "Scegli la cartella che contiene i tuoi progetti Ableton"
        panel.directoryURL = folder ?? FileManager.default.urls(for: .musicDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { response in
            guard response == .OK, let url = panel.url else { return }
            self.folder = url
            self.startServer(folder: url)
        }
    }

    @objc func revealFolder(_ sender: Any?) {
        guard let folder else { return }
        NSWorkspace.shared.activateFileViewerSelecting([folder])
    }

    // MARK: server Python

    private func findPython() -> URL? {
        // le app non vedono il PATH del Terminale: proviamo i percorsi più comuni
        ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
            .map { URL(fileURLWithPath: $0) }
    }

    private func startServer(folder: URL) {
        stopServer()
        guard let script = Bundle.main.url(forResource: "track_tracker", withExtension: "py") else {
            return showMessage("Manca track_tracker.py", "L'app è incompleta: ricompilala con Scripts/build-app.sh.")
        }
        guard let python = findPython() else {
            return showMessage("Python non trovato",
                               "Installa gli strumenti da riga di comando di Apple: apri il Terminale e scrivi xcode-select --install")
        }
        showMessage("Apro i progetti…", folder.path)

        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", script.path, folder.path, "--app"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = env

        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        stderrText = ""

        var buffer = ""
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            buffer += String(decoding: data, as: UTF8.self)
            guard let line = buffer.split(separator: "\n").first(where: { $0.hasPrefix("TRACKTRACKER_URL ") }),
                  let url = URL(string: String(line.dropFirst("TRACKTRACKER_URL ".count))) else { return }
            handle.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self, self.server === process else { return }
                self.serverURL = url
                self.webView.load(URLRequest(url: url))
            }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self?.stderrText = String(((self?.stderrText ?? "") + text).suffix(4000)) }
        }
        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, self.server === proc else { return }  // fermato da noi: tutto ok
                self.server = nil
                self.serverURL = nil
                self.showMessage("Track Tracker si è fermato",
                                 self.stderrText.isEmpty ? "Codice di uscita \(proc.terminationStatus)" : self.stderrText,
                                 button: "Riprova")
            }
        }

        do {
            try process.run()
            server = process
            serverInput = input
        } catch {
            showMessage("Impossibile avviare Python", error.localizedDescription)
        }
    }

    private func stopServer() {
        guard let process = server else { return }
        server = nil
        serverURL = nil
        try? serverInput?.fileHandleForWriting.close()  // lo script esce da solo
        serverInput = nil
        if process.isRunning { process.terminate() }
    }

    @objc func reloadProjects(_ sender: Any?) {
        if serverURL != nil {
            webView.evaluateJavaScript("load(true); toast('Cartella riletta')")
        } else if let folder {
            startServer(folder: folder)
        }
    }

    @objc func focusSearch(_ sender: Any?) {
        webView.evaluateJavaScript("document.getElementById('q')?.focus()")
    }

    // MARK: pagine di messaggio

    private func showMessage(_ title: String, _ detail: String, button: String? = nil) {
        func esc(_ s: String) -> String {
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
        }
        let action = button.map { "<p><a href=\"tracktracker:action\">\(esc($0))</a></p>" } ?? ""
        let html = """
        <!DOCTYPE html><html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#121214;
        color:#ececf0;font:14px/1.5 -apple-system,sans-serif;text-align:center}
        div{max-width:560px;padding:24px}h1{font-size:20px;margin:0 0 10px}
        pre{white-space:pre-wrap;color:#8d8d96;font:12px/1.5 ui-monospace,monospace;text-align:left}
        a{display:inline-block;margin-top:8px;background:#0a84ff;color:#fff;text-decoration:none;
        padding:8px 16px;border-radius:8px}
        </style></head><body><div><h1>\(esc(title))</h1><pre>\(esc(detail))</pre>\(action)</div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: verifica automatica (TrackTracker --check-launch)

    private static let checking = CommandLine.arguments.contains("--check-launch")

    private func startLaunchCheckTimeout() {
        guard Self.checking else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            fputs("Verifica non riuscita: la board non si è caricata entro 20 secondi.\n", stderr)
            exit(1)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard Self.checking, webView.url?.host == "127.0.0.1" else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            // conta colonne e card, e prova un salvataggio (rimette le impostazioni come sono)
            let js = """
                const s = await api('/api/settings', {stale_days: S.settings.stale_days});
                return JSON.stringify({cols: document.querySelectorAll('.col').length,
                    cards: document.querySelectorAll('.card').length,
                    count: document.getElementById('count').textContent, saved: !!s.settings});
                """
            webView.callAsyncJavaScript(js, in: nil, in: .page) { result in
                let text: String
                switch result {
                case .success(let value): text = (value as? String) ?? "\(value)"
                case .failure(let error): text = "errore: \(error.localizedDescription)"
                }
                let ok = text.contains("\"cols\":8") && !text.contains("\"cards\":0") && text.contains("\"saved\":true")
                fputs((ok ? "Avvio verificato: " : "Verifica non riuscita: ") + text + "\n", stderr)
                if ok { NSApp.terminate(nil) } else { exit(1) }
            }
        }
    }

    // MARK: navigazione

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.scheme == "tracktracker" {
            decisionHandler(.cancel)
            if folder == nil { chooseFolder(nil) } else { reloadProjects(nil) }
            return
        }
        if url.scheme == "about" || (url.host == "127.0.0.1" && url.port == serverURL?.port) {
            return decisionHandler(.allow)
        }
        decisionHandler(.cancel)  // qualsiasi link esterno si apre nel browser
        if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
    }

    // MARK: menu

    private func installMenu() {
        let main = NSMenu()

        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            item.submenu = menu
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }

        submenu("Track Tracker", [
            item("Informazioni su Track Tracker", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Nascondi Track Tracker", #selector(NSApplication.hide(_:)), "h"),
            item("Nascondi altre", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Mostra tutte", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Esci da Track Tracker", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Archivio", [
            item("Scegli cartella progetti…", #selector(chooseFolder(_:)), "o"),
            item("Mostra cartella nel Finder", #selector(revealFolder(_:)), "o", [.command, .shift]),
            .separator(),
            item("Rileggi cartella", #selector(reloadProjects(_:)), "r"),
            item("Cerca", #selector(focusSearch(_:)), "f"),
            .separator(),
            item("Chiudi finestra", #selector(NSWindow.performClose(_:)), "w"),
        ])
        // senza questo menu ⌘C, ⌘V, ⌘Z e ⌘A non funzionano nei campi di testo
        submenu("Modifica", [
            item("Annulla", Selector(("undo:")), "z"),
            item("Ripeti", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Taglia", #selector(NSText.cut(_:)), "x"),
            item("Copia", #selector(NSText.copy(_:)), "c"),
            item("Incolla", #selector(NSText.paste(_:)), "v"),
            item("Seleziona tutto", #selector(NSText.selectAll(_:)), "a"),
        ])
        let windowItems = [
            item("Contrai", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Ridimensiona", #selector(NSWindow.performZoom(_:))),
        ]
        submenu("Finestra", windowItems)
        NSApp.windowsMenu = main.items.last?.submenu
        NSApp.mainMenu = main
    }
}
