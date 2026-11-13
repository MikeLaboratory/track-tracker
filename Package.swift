// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TrackTracker",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "TrackTracker", targets: ["TrackTracker"])
    ],
    targets: [
        .executableTarget(name: "TrackTracker")
    ]
)
