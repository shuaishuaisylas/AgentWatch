import Foundation
import Testing
@testable import CapsuleCore

@Suite(.serialized)
final class APIClientTests {
    private let clients = Locked<[APIClient]>([])
    deinit { for client in clients.read() { Task { await client.close() } } }
    private struct Value: Decodable, Equatable { let observedTotal: Int }
    private let json = Data(#"{"observed_total":7}"#.utf8)

    private func client(port: Int = 19_101) throws -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FixtureProtocol.self]
        configuration.httpAdditionalHeaders = ["Cookie": "other=must_not_leak", "X-AgentWatch-CSRF": "must_not_leak"]
        let client = try APIClient(baseURL: URL(string: "http://127.0.0.1:\(port)")!, configuration: configuration)
        clients.modify { $0.append(client) }
        return client
    }

    private func home(cookie: String = "test-cookie", csrf: String = "test-csrf", delay: TimeInterval = 0) -> FixtureProtocol.Reply {
        .response(200, ["Content-Type": "text/html; charset=utf-8", "Set-Cookie": "agentwatch=\(cookie); HttpOnly; SameSite=Strict; Path=/"],
                  Data("<html><head><meta content='\(csrf)' name='csrf'></head></html>".utf8), delay)
    }

    private func ok(delay: TimeInterval = 0) -> FixtureProtocol.Reply {
        .response(200, ["Content-Type": "application/json"], json, delay)
    }

    private func assertError<T>(_ expected: APIClientError, file: StaticString = #filePath, line: UInt = #line,
                                operation: () async throws -> T) async {
        do { _ = try await operation(); recordFailure("Expected a sanitized client error", file: file, line: line) }
        catch let error as APIClientError { expectEqual(error, expected, file: file, line: line) }
        catch { recordFailure("Unexpected error type: \(type(of: error))", file: file, line: line) }
    }

    @Test func testOriginRequiresExactLoopbackAndExplicitValidPort() throws {
        let invalid = ["https://127.0.0.1:8767", "http://localhost:8767", "http://127.0.0.2:8767",
                       "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                       "http://user:password@127.0.0.1:8767", "http://127.0.0.1:8767/",
                       "http://127.0.0.1:8767/api", "http://127.0.0.1:8767?x=1", "http://127.0.0.1:8767#x"]
        for value in invalid {
            guard let url = URL(string: value) else { continue }
            expectThrows(try APIClient(baseURL: url), value) {
                expectEqual($0 as? APIClientError, .invalidBaseURL)
            }
        }
    }

    @Test func testPathAllowlistRejectsOriginEscapesBeforeNetworking() async throws {
        let client = try client()
        FixtureProtocol.install { _ in recordFailure("Rejected routes must not issue any request"); return self.ok() }
        let invalid = ["//127.0.0.1:9000/api/snapshot", "http://127.0.0.1:9000/api/snapshot", "/api/stats",
                       "/api/claude/enable", "/api/../api/snapshot", "/api/snapshot#x", "/api\\snapshot", "/api/snapshot\n"]
        for path in invalid {
            await assertError(.invalidPath) { try await client.get(path, as: Value.self) }
        }
        await assertError(.invalidPath) { try await client.post("/api/jump?kind=codex", as: Value.self) }
        await assertError(.invalidPath) { try await client.post("/api/alias", as: Value.self) }
        for path in ["/api/inbox", "/api/inbox/read/", "/api/inbox/clear", "/api/inbox/read?all=true"] {
            await assertError(.invalidPath) { try await client.post(path, as: Value.self) }
        }
        await assertError(.invalidPath) { try await client.get("/api/inbox/read", as: Value.self) }
        for path in ["/api/attention", "/api/attention/dismiss/", "/api/attention/dismiss?all=true"] {
            await assertError(.invalidPath) { try await client.post(path, as: Value.self) }
        }
        await assertError(.invalidPath) { try await client.get("/api/attention/dismiss", as: Value.self) }
    }

    @Test func testBootstrapAndRequestsUseOnlyManualOriginCredentials() async throws {
        let client = try client()
        let requests = Locked<[URLRequest]>([])
        FixtureProtocol.install { request in
            requests.modify { $0.append(request) }
            expectEqual(request.timeoutInterval, 6)
            expectFalse(request.httpShouldHandleCookies)
            if request.url!.path == "/" {
                expectNil(request.value(forHTTPHeaderField: "Cookie"))
                expectNil(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"))
                return self.home()
            }
            expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=test-cookie")
            if request.httpMethod == "POST" {
                expectEqual(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"), "test-csrf")
                expectEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            } else {
                expectNil(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"))
            }
            return self.ok()
        }
        let value = try await client.get("/api/snapshot?tool=all&project=all&role=main", as: Value.self)
        expectEqual(value, Value(observedTotal: 7))
        _ = try await client.post("/api/jump", body: ["id": "codex:artificial"], as: Value.self)
        _ = try await client.post("/api/rescan", as: Value.self)
        _ = try await client.post("/api/inbox/read", body: ["id": "codex:artificial", "completed_at": "1792029600.123456"], as: Value.self)
        _ = try await client.post("/api/attention/dismiss", body: ["id": "codex:artificial", "version": String(repeating: "a", count: 64)], as: Value.self)
        let sent = requests.read()
        expectEqual(sent.count, 6)
        expectEqual(sent[1].url!.query, "tool=all&project=all&role=main")
        // URLProtocol can expose a body as a stream; both forms are inspected.
        expectEqual(try bodyObject(sent[2]), ["id": "codex:artificial"])
        expectEqual(try bodyObject(sent[3]), [:])
        expectEqual(sent[4].url!.path, "/api/inbox/read")
        expectEqual(try bodyObject(sent[4]), ["id": "codex:artificial", "completed_at": "1792029600.123456"])
        expectEqual(sent[5].url!.path, "/api/attention/dismiss")
        expectEqual(try bodyObject(sent[5]), ["id": "codex:artificial", "version": String(repeating: "a", count: 64)])
    }

    @Test func testQueryItemsRoundTripWithoutBecomingAnotherOrigin() async throws {
        let client = try client()
        let id = "codex:artificial / ? & # 中文"
        var parts = URLComponents()
        parts.path = "/api/children"
        parts.queryItems = [URLQueryItem(name: "id", value: id)]
        FixtureProtocol.install { request in
            if request.url!.path == "/" { return self.home() }
            expectEqual(request.url!.host, "127.0.0.1")
            expectEqual(request.url!.port, 19_101)
            expectEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value, id)
            return self.ok()
        }
        _ = try await client.get(parts.string!, as: Value.self)
    }

    @Test func testAPICookiesAreNotAutomaticallyAccepted() async throws {
        let client = try client()
        let calls = Locked(0)
        FixtureProtocol.install { request in
            if request.url!.path == "/" { return self.home() }
            calls.modify { $0 += 1 }
            expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=test-cookie")
            return .response(200, ["Content-Type": "application/json", "Set-Cookie": "agentwatch=replacement; Path=/, other=secret; Path=/"], self.json, 0)
        }
        _ = try await client.get("/api/snapshot", as: Value.self)
        _ = try await client.get("/api/subscriptions", as: Value.self)
        expectEqual(calls.read(), 2)
    }

    @Test func testTwoPortsHaveIndependentCookiesAndCSRF() async throws {
        let first = try client(port: 19_102)
        let second = try client(port: 19_103)
        let boots = Locked<[Int: Int]>([:])
        FixtureProtocol.install { request in
            let port = request.url!.port!
            if request.url!.path == "/" {
                boots.modify { $0[port, default: 0] += 1 }
                expectNil(request.value(forHTTPHeaderField: "Cookie"))
                return self.home(cookie: "cookie-\(port)", csrf: "csrf-\(port)")
            }
            expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=cookie-\(port)")
            expectEqual(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"), "csrf-\(port)")
            return self.ok()
        }
        async let a = first.post("/api/rescan", as: Value.self)
        async let b = second.post("/api/rescan", as: Value.self)
        _ = try await (a, b)
        expectEqual(boots.read(), [19_102: 1, 19_103: 1])
    }

    @Test func testConcurrentFirstReadsShareOneBootstrap() async throws {
        let client = try client()
        let homes = Locked(0)
        FixtureProtocol.install { request in
            if request.url!.path == "/" {
                homes.modify { $0 += 1 }
                return self.home(delay: 0.04)
            }
            return self.ok()
        }
        async let a = client.get("/api/snapshot", as: Value.self)
        async let b = client.get("/api/subscriptions", as: Value.self)
        _ = try await (a, b)
        expectEqual(homes.read(), 1)
    }

    @Test func testGET401RebootstrapsAndRetriesOnce() async throws {
        let client = try client()
        let counts = Locked((homes: 0, reads: 0))
        FixtureProtocol.install { request in
            if request.url!.path == "/" {
                let generation = counts.modify { state -> Int in state.homes += 1; return state.homes }
                return self.home(cookie: "cookie-\(generation)", csrf: "csrf-\(generation)")
            }
            let read = counts.modify { state -> Int in state.reads += 1; return state.reads }
            if read == 1 { return .response(401, [:], Data(), 0) }
            expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=cookie-2")
            return self.ok()
        }
        _ = try await client.get("/api/snapshot", as: Value.self)
        expectEqual(counts.read().homes, 2)
        expectEqual(counts.read().reads, 2)
    }

    @Test func testCancellingOneWaiterDoesNotDiscardSharedBootstrap() async throws {
        let client = try client()
        let (started, continuation) = AsyncStream<Void>.makeStream()
        let counts = Locked((homes: 0, reads: 0))
        FixtureProtocol.install { request in
            if request.url!.path == "/" {
                counts.modify { $0.homes += 1 }
                continuation.yield(())
                return self.home(delay: 0.05)
            }
            counts.modify { $0.reads += 1 }
            return self.ok()
        }
        let cancelled = Task { try await client.get("/api/snapshot", as: Value.self) }
        for await _ in started { break }
        continuation.finish()
        let kept = Task { try await client.get("/api/subscriptions", as: Value.self) }
        cancelled.cancel()
        do { _ = try await cancelled.value; recordFailure("Cancelled waiter returned data") }
        catch is CancellationError { }
        catch { recordFailure("Cancelled waiter returned unexpected error type") }
        _ = try await kept.value
        _ = try await client.get("/api/snapshot", as: Value.self)
        expectEqual(counts.read().homes, 1)
        expectEqual(counts.read().reads, 2)
    }

    @Test func testRepeatedGET401HasStrictRecoveryLimit() async throws {
        let client = try client()
        let counts = Locked((homes: 0, reads: 0))
        FixtureProtocol.install { request in
            if request.url!.path == "/" { counts.modify { $0.homes += 1 }; return self.home() }
            counts.modify { $0.reads += 1 }
            return .response(401, [:], Data(#"{"error":"artificial"}"#.utf8), 0)
        }
        await assertError(.httpStatus(401)) { try await client.get("/api/snapshot", as: Value.self) }
        expectEqual(counts.read().homes, 2)
        expectEqual(counts.read().reads, 2)
    }

    @Test func testDelayedConcurrent401CannotEraseNewAuthentication() async throws {
        let client = try client()
        let counts = Locked((homes: 0, oldReads: 0, newReads: 0))
        FixtureProtocol.install { request in
            if request.url!.path == "/" {
                let generation = counts.modify { state -> Int in state.homes += 1; return state.homes }
                return self.home(cookie: "cookie-\(generation)", csrf: "csrf-\(generation)", delay: 0.02)
            }
            if request.value(forHTTPHeaderField: "Cookie") == "agentwatch=cookie-1" {
                let number = counts.modify { state -> Int in state.oldReads += 1; return state.oldReads }
                return .response(401, [:], Data(), number == 1 ? 0.02 : 0.15)
            }
            counts.modify { $0.newReads += 1 }
            expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=cookie-2")
            return self.ok()
        }
        async let a = client.get("/api/snapshot", as: Value.self)
        async let b = client.get("/api/subscriptions", as: Value.self)
        _ = try await (a, b)
        expectEqual(counts.read().homes, 2)
        expectEqual(counts.read().oldReads, 2)
        expectEqual(counts.read().newReads, 2)
    }

    @Test func testPOST401IsNeverReplayedOrReauthenticatedInThatAction() async throws {
        let client = try client()
        let counts = Locked((homes: 0, posts: 0))
        FixtureProtocol.install { request in
            if request.url!.path == "/" { counts.modify { $0.homes += 1 }; return self.home() }
            counts.modify { $0.posts += 1 }
            return .response(401, [:], Data(), 0)
        }
        await assertError(.httpStatus(401)) { try await client.post("/api/jump", body: ["id": "artificial"], as: Value.self) }
        expectEqual(counts.read().homes, 1)
        expectEqual(counts.read().posts, 1)
    }

    @Test func testPOSTTimeoutIsNeverReplayed() async throws {
        let client = try client()
        let posts = Locked(0)
        FixtureProtocol.install { request in
            expectEqual(request.timeoutInterval, 6)
            if request.url!.path == "/" { return self.home() }
            posts.modify { $0 += 1 }
            return .failure(URLError(.timedOut), 0)
        }
        await assertError(.network(.timedOut)) { try await client.post("/api/rescan", as: Value.self) }
        expectEqual(posts.read(), 1)
    }

    @Test func testCompletionRead401AndTimeoutNeverReplayTheReceipt() async throws {
        let failures: [(FixtureProtocol.Reply, APIClientError)] = [
            (.response(401, [:], Data(), 0), .httpStatus(401)),
            (.failure(URLError(.timedOut), 0), .network(.timedOut))
        ]
        for (reply, expected) in failures {
            let client = try client()
            let counts = Locked((homes: 0, receipts: 0))
            FixtureProtocol.install { request in
                if request.url!.path == "/" {
                    counts.modify { $0.homes += 1 }
                    return self.home()
                }
                expectEqual(request.url!.path, "/api/inbox/read")
                expectEqual(request.httpMethod, "POST")
                expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=test-cookie")
                expectEqual(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"), "test-csrf")
                counts.modify { $0.receipts += 1 }
                return reply
            }
            await assertError(expected) {
                try await client.post("/api/inbox/read", body: ["id": "codex:artificial", "completed_at": "1792029600.123456"], as: Value.self)
            }
            expectEqual(counts.read().homes, 1)
            expectEqual(counts.read().receipts, 1)
        }
    }
    @Test func testAttentionDismissFailuresNeverReplayTheConfirmation() async throws {
        let failures: [(FixtureProtocol.Reply, APIClientError)] = [
            (.response(401, [:], Data(), 0), .httpStatus(401)),
            (.response(409, [:], Data(), 0), .httpStatus(409)),
            (.failure(URLError(.timedOut), 0), .network(.timedOut))
        ]
        for (reply, expected) in failures {
            let client = try client()
            let counts = Locked((homes: 0, posts: 0))
            FixtureProtocol.install { request in
                if request.url!.path == "/" { counts.modify { $0.homes += 1 }; return self.home() }
                expectEqual(request.url!.path, "/api/attention/dismiss")
                expectEqual(request.httpMethod, "POST")
                expectEqual(request.value(forHTTPHeaderField: "Cookie"), "agentwatch=test-cookie")
                expectEqual(request.value(forHTTPHeaderField: "X-AgentWatch-CSRF"), "test-csrf")
                counts.modify { $0.posts += 1 }
                return reply
            }
            await assertError(expected) {
                try await client.post("/api/attention/dismiss", body: ["id": "codex:artificial", "version": String(repeating: "a", count: 64)], as: Value.self)
            }
            expectEqual(counts.read().homes, 1)
            expectEqual(counts.read().posts, 1)
        }
    }

    @Test func testMissingCookiePreventsAPIRequests() async throws {
        let client = try client()
        FixtureProtocol.install { request in
            expectEqual(request.url!.path, "/")
            return .response(200, ["Content-Type": "text/html", "Set-Cookie": "unrelated=not_saved; Path=/"], Data("<meta name='csrf' content='test'>".utf8), 0)
        }
        await assertError(.bootstrapMissingCookie) { try await client.get("/api/snapshot", as: Value.self) }
    }

    @Test func testMissingOrAmbiguousCSRFPreventsAPIRequests() async throws {
        for html in ["<html>no token</html>", "<meta name='csrf' content=''>",
                     "<meta name='csrf' content='a'><meta name='csrf' content='b'>"] {
            let client = try client()
            FixtureProtocol.install { request in
                expectEqual(request.url!.path, "/")
                return .response(200, ["Content-Type": "text/html", "Set-Cookie": "agentwatch=test-cookie; Path=/"], Data(html.utf8), 0)
            }
            await assertError(.bootstrapMissingCSRF) { try await client.get("/api/snapshot", as: Value.self) }
        }
    }

    @Test func testRedirectResponsesAreRejectedForBootstrapReadsAndActions() async throws {
        for target in ["/", "/api/snapshot", "/api/jump"] {
            let client = try client()
            let seen = Locked<[String]>([])
            FixtureProtocol.install { request in
                seen.modify { $0.append(request.url!.path) }
                if request.url!.path == target {
                    return .response(307, ["Location": "http://127.0.0.1:19999/api/snapshot"], Data(), 0)
                }
                return self.home()
            }
            if target == "/api/jump" {
                await assertError(.redirectRejected) { try await client.post(target, as: Value.self) }
            } else {
                await assertError(.redirectRejected) { try await client.get("/api/snapshot", as: Value.self) }
            }
            expectEqual(seen.read(), target == "/" ? ["/"] : ["/", target])
        }
    }

    @Test(.timeLimit(.minutes(1))) func testCloseInvalidatesInFlightResponseAndFurtherRequests() async throws {
        let client = try client()
        let (started, continuation) = AsyncStream<Void>.makeStream()
        FixtureProtocol.install { request in
            if request.url!.path == "/" { return self.home() }
            continuation.yield(())
            return self.ok(delay: 0.5)
        }
        let pending = Task { try await client.get("/api/snapshot", as: Value.self) }
        for await _ in started { break }
        continuation.finish()
        await client.close()
        await assertError(.closed) { try await pending.value }
        await assertError(.closed) { try await client.get("/api/snapshot", as: Value.self) }
        await client.close()
    }

    @Test func testBootstrapFailureCanBeRetriedByANewRead() async throws {
        let client = try client()
        let homes = Locked(0)
        FixtureProtocol.install { request in
            if request.url!.path == "/" {
                let number = homes.modify { value -> Int in value += 1; return value }
                if number == 1 { return .failure(URLError(.cannotConnectToHost), 0) }
                return self.home()
            }
            return self.ok()
        }
        await assertError(.network(.cannotConnectToHost)) { try await client.get("/api/snapshot", as: Value.self) }
        _ = try await client.get("/api/snapshot", as: Value.self)
        expectEqual(homes.read(), 2)
    }

    @Test func testHTTPAndDecodeFailuresNeverExposeResponseBody() async throws {
        let client = try client()
        let calls = Locked(0)
        FixtureProtocol.install { request in
            if request.url!.path == "/" { return self.home() }
            let number = calls.modify { value -> Int in value += 1; return value }
            return .response(number == 1 ? 403 : 200, ["Content-Type": "application/json"], Data("sensitive-artificial-response".utf8), 0)
        }
        await assertError(.httpStatus(403)) { try await client.get("/api/integrations", as: Value.self) }
        await assertError(.decodingFailed) { try await client.get("/api/snapshot", as: Value.self) }
        expectFalse(APIClientError.httpStatus(403).localizedDescription.contains("sensitive"))
    }
}

private final class Locked<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value
    init(_ value: Value) { self.value = value }
    func read() -> Value { lock.lock(); defer { lock.unlock() }; return value }
    @discardableResult func modify<Result>(_ action: (inout Value) -> Result) -> Result {
        lock.lock(); defer { lock.unlock() }; return action(&value)
    }
}

private final class FixtureProtocol: URLProtocol {
    enum Reply {
        case response(Int, [String: String], Data, TimeInterval)
        case failure(URLError, TimeInterval)
        var delay: TimeInterval {
            switch self { case .response(_, _, _, let delay), .failure(_, let delay): return delay }
        }
    }
    private static let handler = Locked<((URLRequest) -> Reply)?>(nil)
    private let stopped = Locked(false)

    static func install(_ action: @escaping (URLRequest) -> Reply) { handler.modify { $0 = action } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handler.read() else { fatalError("Fixture handler missing") }
        let reply = handler(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + reply.delay) { [self] in
            guard !stopped.read() else { return }
            switch reply {
            case .response(let status, let headers, let data, _):
                let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            case .failure(let error, _):
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }
    override func stopLoading() { stopped.modify { $0 = true } }
}

private func bodyObject(_ request: URLRequest) throws -> [String: String] {
    let data: Data
    if let body = request.httpBody {
        data = body
    } else if let stream = request.httpBodyStream {
        stream.open()
        defer { stream.close() }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            result.append(buffer, count: count)
        }
        data = result
    } else { throw APIClientError.invalidResponse }
    return try JSONSerialization.jsonObject(with: data) as! [String: String]
}

// Small assertion helpers keep failures attached to the originating test line.
private func location(_ file: StaticString, _ line: UInt) -> SourceLocation {
    SourceLocation(fileID: String(describing: file), filePath: String(describing: file), line: Int(line), column: 1)
}

private func recordFailure(_ message: String, file: StaticString = #filePath, line: UInt = #line) {
    Issue.record(Comment(rawValue: message), sourceLocation: location(file, line))
}

private func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String = "",
                                        file: StaticString = #filePath, line: UInt = #line) {
    if actual != expected {
        recordFailure("Expected equality: \(actual) != \(expected). \(message)", file: file, line: line)
    }
}

private func expectNil<T>(_ actual: T?, file: StaticString = #filePath, line: UInt = #line) {
    if actual != nil { recordFailure("Expected nil", file: file, line: line) }
}

private func expectFalse(_ actual: Bool, file: StaticString = #filePath, line: UInt = #line) {
    if actual { recordFailure("Expected false", file: file, line: line) }
}

private func expectThrows<T>(_ expression: @autoclosure () throws -> T, _ message: String = "",
                              file: StaticString = #filePath, line: UInt = #line,
                              errorHandler: (Error) -> Void) {
    do { _ = try expression(); recordFailure("Expected error. " + message, file: file, line: line) }
    catch { errorHandler(error) }
}
