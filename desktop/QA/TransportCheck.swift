import Foundation
#if canImport(CapsuleCore)
import CapsuleCore
#endif

/// Standalone real-URLSession acceptance against owned artificial fixtures only.
/// Build without sharing a Swift Package build directory:
///   xcrun swiftc -target arm64-apple-macosx13.0 \
///     -module-cache-path /private/tmp/agentwatch-transport-module-cache \
///     -parse-as-library desktop/Sources/CapsuleCore/APIClient.swift \
///     desktop/QA/TransportCheck.swift -o /tmp/AgentWatchTransportCheck
///   /tmp/AgentWatchTransportCheck /absolute/path/desktop/QA/transport_fixture.py
@main
struct TransportCheck {
    struct Snapshot: Decodable { let demo: Bool }
    struct Requested: Decodable { let requested: Bool }
    struct Diagnostics: Decodable { let fixture: Bool; let counts: [String: Int] }
    enum Failure: Error, CustomStringConvertible {
        case check(String)
        var description: String { switch self { case .check(let value): return value } }
    }

    static func require(_ condition: Bool, _ description: String) throws {
        if !condition { throw Failure.check(description) }
    }

    static func expectedError<T>(_ expected: APIClientError, _ operation: () async throws -> T) async throws {
        do { _ = try await operation() }
        catch let error as APIClientError {
            try require(error == expected, "Unexpected sanitized API error")
            return
        }
        throw Failure.check("An operation unexpectedly returned data")
    }

    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            print("Usage: AgentWatchTransportCheck /absolute/path/desktop/QA/transport_fixture.py")
            exit(2)
        }
        let fixturePath = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL.path
        guard fixturePath.hasSuffix("/desktop/QA/transport_fixture.py"), FileManager.default.fileExists(atPath: fixturePath) else {
            print("FAIL: Expected the artificial transport fixture script")
            exit(2)
        }
        do {
            try await normal(fixturePath)
            try await recovery(fixturePath)
            try await recoveryLimit(fixturePath)
            try await post401(fixturePath)
            try await postTimeout(fixturePath)
            try await redirects(fixturePath)
            try await close(fixturePath)
            print("PASS: 7 real URLSession fixture checks; no existing service was contacted")
        } catch {
            // The error deliberately omits payloads, headers, and credentials.
            if let known = error as? Failure { print("FAIL: " + known.description) }
            else if let known = error as? APIClientError { print("FAIL: " + known.localizedDescription) }
            else { print("FAIL: Artificial transport check could not complete (\(type(of: error)))") }
            exit(1)
        }
    }

    static func normal(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "normal")
        defer { fixture.stop() }
        let a = try APIClient(baseURL: fixture.origins[0])
        let b = try APIClient(baseURL: fixture.origins[1])
        do {
            async let first = a.post("/api/rescan", as: Requested.self)
            async let second = b.post("/api/rescan", as: Requested.self)
            let requested = try await (first, second)
            try require(requested.0.requested && requested.1.requested, "Both artificial CSRF actions must succeed")
            async let snapshotA = a.get("/api/snapshot?tool=all&project=all&role=main", as: Snapshot.self)
            async let snapshotB = b.get("/api/snapshot?tool=all&project=all&role=main", as: Snapshot.self)
            let snapshots = try await (snapshotA, snapshotB)
            try require(snapshots.0.demo && snapshots.1.demo, "Both artificial origins must identify demo data")
            for index in 0...1 {
                let counts = try await fixture.counts(index)
                try require(counts["GET /"] == 1 && counts["POST /api/rescan"] == 1 && counts["GET /api/snapshot"] == 1,
                            "Each origin must have its own single bootstrap, authenticated GET, and CSRF POST")
            }
            await a.close(); await b.close()
        } catch {
            await a.close(); await b.close()
            throw error
        }
        print("PASS: two actual ports keep cookies and CSRF isolated")
    }

    static func recovery(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "restart")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        do {
            let snapshot = try await client.get("/api/snapshot", as: Snapshot.self)
            try require(snapshot.demo, "Artificial restart recovery returned a non-demo snapshot")
            let result = try await client.post("/api/rescan", as: Requested.self)
            try require(result.requested, "Restart recovery must refresh the CSRF value as well as cookie")
            let counts = try await fixture.counts(0)
            try require(counts["GET /"] == 2 && counts["GET /api/snapshot"] == 2 && counts["POST /api/rescan"] == 1,
                        "GET recovery must bootstrap once and retry once")
            await client.close()
        } catch { await client.close(); throw error }
        print("PASS: restart 401 recovers cookie and CSRF with one GET retry")
    }

    static func recoveryLimit(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "always401")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        do {
            try await expectedError(.httpStatus(401)) { try await client.get("/api/snapshot", as: Snapshot.self) }
            let counts = try await fixture.counts(0)
            try require(counts["GET /"] == 2 && counts["GET /api/snapshot"] == 2,
                        "Persistent 401 must stop after the second GET")
            await client.close()
        } catch { await client.close(); throw error }
        print("PASS: persistent GET 401 has a strict retry limit")
    }

    static func post401(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "post401")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        do {
            try await expectedError(.httpStatus(401)) { try await client.post("/api/rescan", as: Requested.self) }
            let counts = try await fixture.counts(0)
            try require(counts["GET /"] == 1 && counts["POST /api/rescan"] == 1,
                        "POST 401 must not bootstrap again or replay the action")
            await client.close()
        } catch { await client.close(); throw error }
        print("PASS: POST 401 is received once and never replayed")
    }

    static func postTimeout(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "post-timeout")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        do {
            let start = ContinuousClock.now
            try await expectedError(.network(.timedOut)) { try await client.post("/api/rescan", as: Requested.self) }
            let duration = start.duration(to: .now)
            try require(duration >= .seconds(5) && duration < .seconds(7.5), "POST timeout must enforce the six-second budget")
            let counts = try await fixture.counts(0)
            try require(counts["GET /"] == 1 && counts["POST /api/rescan"] == 1,
                        "Timed-out POST must be received exactly once")
            await client.close()
        } catch { await client.close(); throw error }
        print("PASS: actual six-second POST timeout does not replay an ambiguous action")
    }

    static func redirects(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "redirect")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        do {
            try await expectedError(.redirectRejected) { try await client.get("/api/snapshot", as: Snapshot.self) }
            let source = try await fixture.counts(0)
            let target = try await fixture.counts(1)
            try require(source["GET /api/snapshot"] == 1 && target["GET /__fixture__/redirect-target", default: 0] == 0,
                        "A real cross-port redirect must never reach the target")
            await client.close()
        } catch { await client.close(); throw error }
        print("PASS: real cross-port redirect is refused before the target receives a request")
    }

    static func close(_ path: String) async throws {
        let fixture = try FixtureProcess(script: path, scenario: "slow-snapshot")
        defer { fixture.stop() }
        let client = try APIClient(baseURL: fixture.origins[0])
        let pending = Task { try await client.get("/api/snapshot", as: Snapshot.self) }
        do {
            var started = false
            for _ in 0..<100 {
                let counts = try await fixture.counts(0)
                if counts["GET /api/snapshot"] == 1 { started = true; break }
                try await Task.sleep(for: .milliseconds(20))
            }
            try require(started, "Artificial delayed response never started")
            await client.close()
            try await expectedError(.closed) { try await pending.value }
            // Keep the owned server alive until it attempts its deliberately late
            // response. Neither that response nor a new call may revive the client.
            try await Task.sleep(for: .seconds(7.6))
            try await expectedError(.closed) { try await client.get("/api/snapshot", as: Snapshot.self) }
            let counts = try await fixture.counts(0)
            try require(counts["GET /api/snapshot"] == 1, "Closed client issued an additional request")
        } catch { pending.cancel(); await client.close(); throw error }
        print("PASS: close cancels in-flight work and remains closed after an actual late response")
    }

    final class FixtureProcess {
        let origins: [URL]
        private let process: Process
        private let diagnostics: URLSession
        private var stopped = false

        init(script: String, scenario: String) throws {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [script, "--scenario", scenario, "--port", "0", "--second-port", "0"]
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            let output = Pipe()
            process.standardOutput = output
            process.standardError = Pipe()
            try process.run()
            do {
                var data = Data()
                while data.count < 8192, let byte = try output.fileHandleForReading.read(upToCount: 1), !byte.isEmpty {
                    if byte.first == 10 { break }
                    data.append(byte)
                }
                struct Start: Decodable { let fixture: Bool; let scenario: String; let origins: [String] }
                let start = try JSONDecoder().decode(Start.self, from: data)
                let urls = start.origins.compactMap(URL.init(string:))
                try require(start.fixture && start.scenario == scenario && urls.count == 2 && urls[0] != urls[1],
                            "Owned fixture startup identity mismatch")
                for url in urls {
                    let check = try APIClient(baseURL: url)
                    Task { await check.close() }
                }
                origins = urls
            } catch {
                if process.isRunning { process.terminate(); process.waitUntilExit() }
                throw Failure.check("Owned artificial fixture could not start")
            }
            self.process = process
            let configuration = URLSessionConfiguration.ephemeral
            configuration.httpShouldSetCookies = false
            configuration.httpCookieStorage = nil
            configuration.urlCredentialStorage = nil
            configuration.urlCache = nil
            configuration.timeoutIntervalForRequest = 2
            configuration.timeoutIntervalForResource = 2
            diagnostics = URLSession(configuration: configuration)
        }

        func counts(_ index: Int) async throws -> [String: Int] {
            var parts = URLComponents(url: origins[index], resolvingAgainstBaseURL: false)!
            parts.path = "/__fixture__/counts"
            let (data, response) = try await diagnostics.data(from: parts.url!)
            try require((response as? HTTPURLResponse)?.statusCode == 200, "Artificial diagnostic request failed")
            let result = try JSONDecoder().decode(Diagnostics.self, from: data)
            try require(result.fixture, "Unexpected server: artificial fixture marker missing")
            return result.counts
        }

        func stop() {
            guard !stopped else { return }
            stopped = true
            diagnostics.invalidateAndCancel()
            if process.isRunning { process.terminate(); process.waitUntilExit() }
        }

        deinit { stop() }
    }
}
