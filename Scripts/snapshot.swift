// Fa uno screenshot di una pagina con WebKit (lo stesso motore dell'app), senza finestre visibili.
// Uso: snapshot <url> <file.png> <larghezza> <altezza> [javascript da eseguire prima]
// Il JavaScript può usare await. L'immagine esce a risoluzione retina (2×).
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 5, let url = URL(string: args[1]),
      let width = Double(args[3]), let height = Double(args[4]) else {
    fputs("Uso: snapshot <url> <file.png> <larghezza> <altezza> [javascript]\n", stderr)
    exit(2)
}
let output = URL(fileURLWithPath: args[2])
let script = args.count > 5 ? args[5] : ""

final class Shooter: NSObject, WKNavigationDelegate {
    let window: NSWindow
    let web: WKWebView
    let url: URL, output: URL, width: Double, height: Double, script: String

    init(url: URL, output: URL, width: Double, height: Double, script: String) {
        self.url = url; self.output = output; self.width = width; self.height = height; self.script = script
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()  // niente preferenze salvate da altre sessioni
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: height), configuration: config)
        // finestra trasparente (alpha 0): serve solo a far disegnare la pagina in 2×
        window = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        super.init()
        window.contentView = web
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.orderFrontRegardless()
        web.navigationDelegate = self
        web.load(URLRequest(url: url))
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { self.fail("tempo scaduto") }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {  // dati caricati dalla pagina
            guard !self.script.isEmpty else { return self.shoot() }
            webView.callAsyncJavaScript(self.script, arguments: [:], in: nil, in: .page) { result in
                if case .failure(let error) = result { return self.fail("JavaScript: \(error)") }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { self.shoot() }  // animazioni
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fail(error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        fail(error.localizedDescription)
    }

    func shoot() {
        let config = WKSnapshotConfiguration()
        config.rect = web.bounds
        web.takeSnapshot(with: config) { image, error in
            guard let image else { return self.fail(error?.localizedDescription ?? "snapshot vuoto") }
            let px = NSSize(width: self.width * 2, height: self.height * 2)
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(px.width), pixelsHigh: Int(px.height),
                                             bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                             colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else {
                return self.fail("bitmap")
            }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            NSGraphicsContext.current?.imageInterpolation = .high
            image.draw(in: NSRect(origin: .zero, size: px))
            NSGraphicsContext.restoreGraphicsState()
            do {
                try rep.representation(using: .png, properties: [:])?.write(to: self.output)
                print("Salvato \(self.output.lastPathComponent) (\(Int(px.width))×\(Int(px.height)))")
                exit(0)
            } catch {
                self.fail(error.localizedDescription)
            }
        }
    }

    func fail(_ message: String) {
        fputs("Screenshot non riuscito: \(message)\n", stderr)
        exit(1)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let shooter = Shooter(url: url, output: output, width: width, height: height, script: script)
withExtendedLifetime(shooter) { app.run() }
