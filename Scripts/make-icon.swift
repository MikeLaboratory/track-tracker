// Disegna l'icona dell'app in un PNG 1024×1024: una forma d'onda le cui barre attraversano
// i colori degli stati (idea → in lavorazione → arrangiamento → mix → master → finita),
// cioè il percorso di una traccia dall'idea all'uscita.
// Uso: swift Scripts/make-icon.swift Resources/AppIcon.png
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
let size = 1024
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

func rgb(_ hex: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: a)
}

// un colore di stato per barra, nello stesso ordine della board (niente sfumature intermedie,
// che tra viola e arancio diventerebbero un rosa spento)
let barColors: [NSColor] = [0x8e8e93, 0x0a84ff, 0x0a84ff, 0xbf5af2, 0xbf5af2, 0xff9f0a, 0xff9f0a,
                            0xffd60a, 0xffd60a, 0x30d158, 0x30d158].map { rgb($0) }

// quadrato arrotondato secondo la griglia delle icone macOS (824 px, margine 100)
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = NSBezierPath(roundedRect: tile, xRadius: 186, yRadius: 186)

NSGraphicsContext.saveGraphicsState()
let drop = NSShadow()
drop.shadowColor = rgb(0x000000, 0.5)
drop.shadowOffset = NSSize(width: 0, height: -14)
drop.shadowBlurRadius = 30
drop.set()
rgb(0x16161a).setFill()
tilePath.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
tilePath.addClip()
NSGradient(colors: [rgb(0x2e2e37), rgb(0x18181d), rgb(0x0b0b0e)])!.draw(in: tile, angle: -90)
// alone colorato dietro l'onda
let glow = NSGradient(colors: [rgb(0x6a5cff, 0.30), rgb(0x6a5cff, 0.0)])!
glow.draw(fromCenter: NSPoint(x: 470, y: 520), radius: 0, toCenter: NSPoint(x: 470, y: 520), radius: 430, options: [])
let glow2 = NSGradient(colors: [rgb(0x30d158, 0.16), rgb(0x30d158, 0.0)])!
glow2.draw(fromCenter: NSPoint(x: 720, y: 470), radius: 0, toCenter: NSPoint(x: 720, y: 470), radius: 300, options: [])
// riflesso in alto
NSGradient(colors: [rgb(0xffffff, 0.10), rgb(0xffffff, 0.0)])!
    .draw(in: NSRect(x: tile.minX, y: tile.maxY - 260, width: tile.width, height: 260), angle: -90)
NSGraphicsContext.restoreGraphicsState()

// bordo sottile per staccare dal Dock scuro
NSGraphicsContext.saveGraphicsState()
tilePath.addClip()
rgb(0xffffff, 0.10).setStroke()
let rim = NSBezierPath(roundedRect: tile.insetBy(dx: 1.5, dy: 1.5), xRadius: 185, yRadius: 185)
rim.lineWidth = 3
rim.stroke()
NSGraphicsContext.restoreGraphicsState()

// forma d'onda
let heights: [CGFloat] = [0.20, 0.38, 0.62, 0.46, 0.82, 1.00, 0.72, 0.56, 0.86, 0.50, 0.30]
let barW: CGFloat = 42, gap: CGFloat = 20
let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
let left = 512 - total / 2, midY: CGFloat = 512, maxH: CGFloat = 520
for (i, h) in heights.enumerated() {
    let c = barColors[i]
    let bh = max(barW, maxH * h)
    let r = NSRect(x: left + CGFloat(i) * (barW + gap), y: midY - bh / 2, width: barW, height: bh)
    let bar = NSBezierPath(roundedRect: r, xRadius: barW / 2, yRadius: barW / 2)
    NSGraphicsContext.saveGraphicsState()
    let s = NSShadow()
    s.shadowColor = c.withAlphaComponent(0.55)
    s.shadowBlurRadius = 34
    s.shadowOffset = .zero
    s.set()
    c.setFill()
    bar.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGraphicsContext.saveGraphicsState()
    bar.addClip()
    NSGradient(colors: [c.blended(withFraction: 0.22, of: .white)!, c, c.blended(withFraction: 0.22, of: .black)!])!
        .draw(in: r, angle: -90)
    NSGraphicsContext.restoreGraphicsState()
}

NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: out))
print("Icona disegnata: \(out)")
