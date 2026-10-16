// swift-tools-version: 5.9
import PackageDescription
let package = Package(
    name: "AgentWatchCapsule",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "AgentWatchCapsule", targets: ["AgentWatchCapsule"]), .library(name: "CapsuleCore", targets: ["CapsuleCore"])],
    targets: [
        .target(name: "CapsuleCore"),
        .executableTarget(name: "AgentWatchCapsule", dependencies: ["CapsuleCore"]),
        .testTarget(name: "CapsuleCoreTests", dependencies: ["CapsuleCore"])
    ])
