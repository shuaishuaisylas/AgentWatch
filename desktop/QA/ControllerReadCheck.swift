import Foundation

/// Artificial transport/controller integration. No NSApplication, WindowServer,
/// Python process, listening socket, real source, or real navigation is created.
@main
@MainActor
struct ControllerReadCheck {
    private struct Failure: Error { let message: String }
    private static var checks = 0
    nonisolated private static let origin = URL(string: "http://127.0.0.1:19483")!
    nonisolated private static let at: TimeInterval = 1_800_000_000

    static func main() async {
        do {
            try await preciseJumpReadsAfterSuccess()
            try await failedOrAmbiguousJumpsNeverRead()
            try await humanAndApplicationJumpsDoNotRead()
            try await manualReadWorksInDemo()
            try await failedReceiptDoesNotClearOrReplay()
            try await preReceiptSnapshotCannotRestoreUnread()
            try await lateReceiptKeepsNewCompletion()
            try await explicitAttentionDismissUpdatesBeforeNewSnapshot()
            try await invalidAttentionCardsCannotDismiss()
            try await failedAttentionDismissPreservesAndNeverReplays()
            try await preDismissSnapshotCannotRestoreOldBatch()
            try await lateDismissKeepsNewHumanRequest()
            try await successfulDismissSurvivesFailedRefresh()
            print("Controller read integration passed: \(checks) checks; artificial URLProtocol only.")
            print("Verified ordered jump/read, explicit human-reminder confirmation, failure non-replay, and response-version isolation.")
        } catch let error as Failure {
            fputs("Controller read integration failed: \(error.message)\n", stderr)
            exit(1)
        } catch {
            fputs("Controller read integration failed: unexpected artificial test error\n", stderr)
            exit(1)
        }
    }

    private static func human(version: String? = String(repeating: "a", count: 64), role: String = "main") -> SessionCard {
        SessionCard(id: "codex:artificial-human", tool: "codex", state: "waiting_user", role: role,
                    jump: JumpInfo(available: true, label: "打开会话", precision: "指定会话", kind: "codex"),
                    attention: SessionAttention(pending: true, waitingUser: 1, dismissVersion: version),
                    inbox: SessionInbox(unreadCompleted: false))
    }
    private static func completed(version: TimeInterval = at - 60, precision: String = "指定会话", kind: String = "codex") -> SessionCard {
        SessionCard(id: "codex:artificial-completed", tool: "codex", state: "idle", role: "main",
                    jump: JumpInfo(available: true, label: "打开会话", precision: precision, kind: kind),
                    attention: SessionAttention(pending: false), lastCompletedAt: version,
                    inbox: SessionInbox(unreadCompleted: true, completedAt: version))
    }
    private static func snapshot(demo: Bool = false, card: SessionCard? = nil, humanCard: SessionCard? = nil) -> Snapshot {
        let done = card ?? completed()
        return Snapshot(demo: demo, now: at,
                        counts: Counts(active: 0, waiting: 1, error: 0, unknown: 0),
                        childrenCounts: Counts(active: 0, waiting: 0, error: 0, unknown: 0, total: 0),
                        sessions: [humanCard ?? human(), done], attentionCounts: AttentionCounts(pending: 1, waitingUser: 1),
                        recentCompleted: [done], inboxCounts: InboxCounts(pending: 2, actionRequired: 1, unreadCompleted: 1))
    }

    @MainActor
    private struct Harness {
        let controller: ClientController
        let connection: APIClient
        let fixture: ReadFixture
        init(demo: Bool = false, card: SessionCard? = nil) throws {
            fixture = ReadFixture(snapshot: ControllerReadCheck.snapshot(demo: demo, card: card))
            ReadProtocol.install { [fixture] request in fixture.reply(request) }
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [ReadProtocol.self]
            connection = try APIClient(baseURL: origin, configuration: configuration)
            controller = ClientController(baseURL: origin, expectDemo: demo, probe: false, connection: connection)
            controller.snapshot = fixture.snapshot
            controller.lastSuccess = ProcessInfo.processInfo.systemUptime
        }
        func close() async { controller.stop(); await connection.close() }
    }

    private static func preciseJumpReadsAfterSuccess() async throws {
        let h = try Harness()
        defer { h.controller.stop() }
        h.fixture.configure { $0.snapshotDelay = 0.3 }
        h.controller.performJump(completed())
        try await wait("Precise jump action completes") { !h.controller.actionBusy }
        try require(h.fixture.posts.map(\.path) == ["/api/jump", "/api/inbox/read"], "Successful precise jump precedes a distinct receipt")
        try require(h.fixture.posts[0].body == ["id": completed().id], "Jump does not carry or execute a read implicitly")
        try require(h.fixture.posts[1].body == ["id": completed().id, "completed_at": String(at - 60)], "Receipt acknowledges the clicked completion version")
        try require(h.controller.snapshot?.recentCompleted?.isEmpty == true && h.controller.status.pending == 1,
                    "Receipt immediately removes the unread card and decreases the complete root count")
        try require(h.controller.snapshot?.sessions.first(where: { $0.id == human().id })?.requiresHumanAttention == true &&
                    h.controller.snapshot?.attentionCounts?.pending == 1, "Reading a completion preserves outstanding human action")
        try require(h.controller.actionMessage == "已标为看过", "Successful receipt is visible before the delayed refresh")
        await h.close()
    }

    private static func failedOrAmbiguousJumpsNeverRead() async throws {
        for mode in [ReadFixture.JumpMode.httpFailure, .timeout, .notOpened, .wrongKind] {
            let h = try Harness()
            h.fixture.configure { $0.jumpMode = mode }
            h.controller.performJump(completed())
            try await wait("Failed or ambiguous jump action settles") { !h.controller.actionBusy }
            try require(h.fixture.posts.map(\.path) == ["/api/jump"], "Failed, timed out, unopened, or mismatched jump is neither read nor replayed")
            try require(h.controller.snapshot?.sessions.first(where: { $0.id == completed().id })?.unreadCompleted == true &&
                        h.controller.status.pending == 2, "Unconfirmed navigation cannot clear completion state")
            if mode == .timeout || mode == .httpFailure {
                try require(h.controller.actionMessage?.contains("未自动重试") == true, "Action failure remains explicitly unconfirmed")
            }
            await h.close()
        }
    }

    private static func humanAndApplicationJumpsDoNotRead() async throws {
        let person = try Harness()
        person.controller.performJump(human())
        try await wait("Human-action navigation settles") { !person.controller.actionBusy }
        try require(person.fixture.posts.map(\.path) == ["/api/jump"], "Human-action navigation never marks a completion read")
        try require(person.controller.snapshot?.attentionCounts?.pending == 1, "Human request stays pending after navigation")
        await person.close()

        let application = try Harness(card: completed(precision: "应用", kind: "claude"))
        application.fixture.configure { $0.jumpKind = "claude" }
        application.controller.performJump(completed(precision: "应用", kind: "claude"))
        try await wait("Application-only navigation settles") { !application.controller.actionBusy }
        try require(application.fixture.posts.map(\.path) == ["/api/jump"], "Application-only navigation does not claim the original session was viewed")
        try require(application.controller.status.pending == 2, "Application-only navigation retains the unread count")
        await application.close()
    }

    private static func manualReadWorksInDemo() async throws {
        let h = try Harness(demo: true)
        defer { h.controller.stop() }
        h.controller.markCompletionRead(id: completed().id, through: at - 60)
        try await wait("Manual demo receipt completes") { !h.controller.actionBusy }
        try require(h.fixture.posts.map(\.path) == ["/api/inbox/read"], "Manual demo read sends no navigation")
        try require(h.controller.status.pending == 1 && h.controller.snapshot?.recentCompleted?.isEmpty == true,
                    "Manual demo read clears the artificial completion only")
        try require(h.controller.snapshot?.attentionCounts?.pending == 1, "Manual read cannot approve or answer a human request")
        await h.close()
    }

    private static func failedReceiptDoesNotClearOrReplay() async throws {
        let h = try Harness()
        defer { h.controller.stop() }
        h.fixture.configure { $0.receiptFailure = true }
        h.controller.performJump(completed())
        try await wait("Failed receipt action settles") { !h.controller.actionBusy }
        try require(h.fixture.posts.map(\.path) == ["/api/jump", "/api/inbox/read"], "Receipt failure does not replay navigation or the receipt")
        try require(h.controller.status.pending == 2 && h.controller.snapshot?.recentCompleted?.count == 1,
                    "A failed receipt keeps the unread completion and count")
        await h.close()
    }

    private static func preReceiptSnapshotCannotRestoreUnread() async throws {
        let h = try Harness()
        defer { h.controller.stop() }
        h.fixture.configure { $0.firstSnapshotDelay = 0.2 }
        h.controller.tick(force: true)
        try await wait("Old snapshot request enters transport") { h.fixture.snapshotRequests > 0 }
        h.controller.performJump(completed())
        try await wait("Receipt settles with an earlier GET in flight") { !h.controller.actionBusy }
        try await wait("Replacement snapshot is requested") { h.fixture.snapshotRequests >= 2 }
        try await Task.sleep(nanoseconds: 260_000_000)
        try require(h.controller.status.pending == 1 && h.controller.snapshot?.recentCompleted?.isEmpty == true,
                    "The pre-receipt GET cannot restore an acknowledged unread card")
        try require(h.fixture.posts.map(\.path) == ["/api/jump", "/api/inbox/read"], "Refresh isolation does not replay either action")
        await h.close()
    }

    private static func lateReceiptKeepsNewCompletion() async throws {
        let h = try Harness()
        defer { h.controller.stop() }
        h.fixture.configure { $0.receiptDelay = 0.15 }
        h.controller.performJump(completed())
        try await wait("Old-version receipt enters transport") { h.fixture.posts.contains(where: { $0.path == "/api/inbox/read" }) }
        let next = snapshot(card: completed(version: at))
        h.fixture.replaceSnapshot(next)
        h.controller.snapshot = next
        try await wait("Late old-version receipt settles") { !h.controller.actionBusy }
        try require(h.controller.snapshot?.sessions.first(where: { $0.id == completed().id })?.inbox?.completedAt == at &&
                    h.controller.snapshot?.sessions.first(where: { $0.id == completed().id })?.unreadCompleted == true,
                    "Late acknowledgment of an old version preserves a newer unread completion")
        try require(h.controller.status.pending == 2 && h.controller.snapshot?.attentionCounts?.pending == 1,
                    "Late receipt preserves the new completion and independent human count")
        await h.close()
    }

    private static func explicitAttentionDismissUpdatesBeforeNewSnapshot() async throws {
        let h = try Harness(demo: true)
        h.fixture.configure { $0.snapshotDelay = 0.12 }
        h.controller.confirmNoPending(human())
        try await wait("Explicit human reminder confirmation settles") { !h.controller.actionBusy }
        try require(h.fixture.posts.map(\.path) == ["/api/attention/dismiss"], "Explicit confirmation never navigates, answers, approves, or marks completion read")
        try require(h.fixture.posts.first?.body == ["id": human().id, "version": String(repeating: "a", count: 64)],
                    "Confirmation carries only the clicked root and opaque request-batch version")
        try require(h.controller.status.pending == 1 && h.controller.snapshot?.attentionCounts?.pending == 0 &&
                    h.controller.snapshot?.sessions.first(where: { $0.id == human().id })?.requiresHumanAttention == false,
                    "A successful dismissal immediately removes the clicked human batch and decrements the root count before GET returns")
        try require(h.controller.actionMessage == "已移除所选本地提醒", "Success describes only the selected local reminder")
        try await wait("Replacement GET is requested after projection") { h.fixture.snapshotRequests > 0 }
        try require(h.controller.status.pending == 1 && h.controller.snapshot?.recentCompleted?.count == 1,
                    "Fresh snapshot retains independent unread completion")
        await h.close()
    }

    private static func invalidAttentionCardsCannotDismiss() async throws {
        let h = try Harness()
        for card in [human(version: nil), human(version: "invalid"), human(role: "child"), completed()] {
            h.controller.confirmNoPending(card)
        }
        try require(!h.controller.actionBusy && h.fixture.posts.isEmpty, "Legacy, malformed, child, and completion cards cannot send a human-reminder confirmation")
        await h.close()
    }

    private static func failedAttentionDismissPreservesAndNeverReplays() async throws {
        for mode in [ReadFixture.DismissMode.httpFailure, .unauthorized, .conflict, .timeout, .notConfirmed] {
            let h = try Harness()
            h.fixture.configure { $0.dismissMode = mode; $0.snapshotDelay = 0.2 }
            h.controller.confirmNoPending(human())
            try await wait("Failed human confirmation settles") { !h.controller.actionBusy }
            try require(h.fixture.posts.map(\.path) == ["/api/attention/dismiss"], "Failed, expired, 401, or timed-out confirmation is never replayed")
            try require(h.controller.status.pending == 2 && h.controller.snapshot?.attentionCounts?.pending == 1,
                        "Unconfirmed dismissal keeps the old human request and count")
            try require(h.controller.actionMessage?.contains("刷新确认") == true, "Unconfirmed dismissal asks for a refreshed state")
            if mode == .conflict {
                try require(h.controller.actionMessage?.contains("提醒已变化") == true, "Version conflict explains why the reminder is preserved")
            }
            await h.close()
        }
    }

    private static func preDismissSnapshotCannotRestoreOldBatch() async throws {
        let h = try Harness()
        h.fixture.configure { $0.firstSnapshotDelay = 0.2 }
        h.controller.tick(force: true)
        try await wait("Pre-confirmation GET enters transport") { h.fixture.snapshotRequests > 0 }
        h.controller.confirmNoPending(human())
        try await wait("Human confirmation settles with an old GET in flight") { !h.controller.actionBusy }
        try await wait("Dismissal launches replacement snapshot") { h.fixture.snapshotRequests >= 2 }
        try await Task.sleep(nanoseconds: 260_000_000)
        try require(h.controller.status.pending == 1 && h.controller.snapshot?.attentionCounts?.pending == 0,
                    "Old in-flight snapshot cannot restore a dismissed request batch")
        try require(h.fixture.posts.count == 1, "Old snapshot isolation never replays dismissal")
        await h.close()
    }

    private static func lateDismissKeepsNewHumanRequest() async throws {
        let h = try Harness()
        h.fixture.configure { $0.dismissDelay = 0.15; $0.snapshotDelay = 0.05 }
        h.controller.confirmNoPending(human())
        try await wait("Old batch confirmation enters transport") { h.fixture.posts.contains(where: { $0.path == "/api/attention/dismiss" }) }
        let next = snapshot(humanCard: human(version: String(repeating: "b", count: 64)))
        h.fixture.replaceSnapshot(next)
        h.controller.tick(force: true)
        try await wait("A newer human batch arrives through GET before the old POST acknowledgment") {
            h.controller.snapshot?.sessions.first(where: { $0.id == human().id })?.attentionDismissVersion == String(repeating: "b", count: 64)
        }
        try await wait("Old batch acknowledgment settles") { !h.controller.actionBusy }
        try require(h.controller.snapshot?.sessions.first(where: { $0.id == human().id })?.attentionDismissVersion == String(repeating: "b", count: 64),
                    "Late acknowledgment cannot swallow a later human request version")
        try await wait("Fresh snapshot keeps newer human request") { h.fixture.snapshotRequests > 0 }
        try require(h.controller.status.pending == 2 && h.controller.snapshot?.attentionCounts?.pending == 1,
                    "New human requests remain in the pending root count")
        await h.close()
    }

    private static func successfulDismissSurvivesFailedRefresh() async throws {
        let h = try Harness()
        h.fixture.configure { $0.snapshotFailure = true }
        h.controller.confirmNoPending(human())
        try await wait("Acknowledged dismissal completes even when fresh GET will fail") { !h.controller.actionBusy }
        try await wait("The replacement GET reports its independent failure") { h.controller.snapshotError != nil }
        try require(h.controller.snapshot?.inboxCounts?.pending == 1 && h.controller.snapshot?.attentionCounts?.pending == 0,
                    "A failed replacement GET retains the already acknowledged local projection")
        try require(h.controller.snapshot?.sessions.first(where: { $0.id == human().id })?.requiresHumanAttention == false &&
                    h.controller.snapshot?.recentCompleted?.count == 1, "Refresh failure cannot restore dismissed human reminders or clear completion")
        try require(h.fixture.posts.map(\.path) == ["/api/attention/dismiss"], "Refresh failure never replays the destructive reminder action")
        await h.close()
    }

    private static func wait(_ label: String, condition: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 2
        while !condition() {
            guard ProcessInfo.processInfo.systemUptime < deadline else { throw Failure(message: label + " timed out") }
            try await Task.sleep(nanoseconds: 2_000_000)
        }
        checks += 1
    }
    private static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        guard condition() else { throw Failure(message: message) }
        checks += 1
    }
}

private final class ReadLock<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value
    init(_ value: Value) { self.value = value }
    func read() -> Value { lock.lock(); defer { lock.unlock() }; return value }
    @discardableResult func modify<Result>(_ action: (inout Value) -> Result) -> Result {
        lock.lock(); defer { lock.unlock() }; return action(&value)
    }
}

private final class ReadFixture: @unchecked Sendable {
    enum JumpMode { case success, httpFailure, timeout, notOpened, wrongKind }
    enum DismissMode { case success, httpFailure, unauthorized, conflict, timeout, notConfirmed }
    struct Sent { let path: String; let body: [String: String] }
    struct State {
        var snapshot: Snapshot
        var posts: [Sent] = []
        var snapshotRequests = 0
        var snapshotFailure = false
        var jumpMode = JumpMode.success
        var jumpKind = "codex"
        var receiptFailure = false
        var receiptDelay: TimeInterval = 0
        var snapshotDelay: TimeInterval = 0
        var firstSnapshotDelay: TimeInterval = 0
        var dismissMode = DismissMode.success
        var dismissDelay: TimeInterval = 0
    }
    private let state: ReadLock<State>
    init(snapshot: Snapshot) { state = ReadLock(State(snapshot: snapshot)) }
    var snapshot: Snapshot { state.read().snapshot }
    var posts: [Sent] { state.read().posts }
    var snapshotRequests: Int { state.read().snapshotRequests }
    func configure(_ mutate: (inout State) -> Void) { state.modify(mutate) }
    func replaceSnapshot(_ snapshot: Snapshot) { state.modify { $0.snapshot = snapshot } }

    func reply(_ request: URLRequest) -> ReadProtocol.Reply {
        guard request.url?.host == "127.0.0.1", request.url?.port == 19483 else { return .failure(.badURL) }
        let path = request.url!.path
        if path == "/" {
            return .response(200, ["Content-Type": "text/html", "Set-Cookie": "agentwatch=artificial; Path=/; HttpOnly; SameSite=Strict"],
                             Data("<meta name='csrf' content='artificial-controller-csrf'>".utf8), 0)
        }
        return state.modify { current in
            if request.httpMethod == "POST" {
                let body = Self.body(request)
                current.posts.append(Sent(path: path, body: body))
                if path == "/api/jump" {
                    switch current.jumpMode {
                    case .httpFailure: return Self.json(["error": "artificial failure"], status: 500)
                    case .timeout: return .failure(.timedOut)
                    case .notOpened: return Self.json(["opened": false, "kind": current.jumpKind])
                    case .wrongKind: return Self.json(["opened": true, "kind": "artificial-mismatched-kind"])
                    case .success: return Self.json(["opened": true, "kind": current.jumpKind])
                    }
                }
                if path == "/api/inbox/read" {
                    if current.receiptFailure { return Self.json(["error": "artificial receipt failure"], status: 500) }
                    guard let id = body["id"], let at = body["completed_at"].flatMap(Double.init) else { return Self.json(["error": "invalid artificial version"], status: 400) }
                    current.snapshot = current.snapshot.applyingCompletionRead(id: id, through: at)
                    return Self.json(["read_completed": true], delay: current.receiptDelay)
                }
                if path == "/api/attention/dismiss" {
                    switch current.dismissMode {
                    case .httpFailure: return Self.json(["error": "artificial failure"], status: 500)
                    case .unauthorized: return Self.json(["error": "artificial auth expiry"], status: 401)
                    case .conflict: return Self.json(["error": "artificial changed batch"], status: 409)
                    case .timeout: return .failure(.timedOut)
                    case .notConfirmed: return Self.json(["dismissed": false])
                    case .success: break
                    }
                    guard let id = body["id"], let version = body["version"],
                          current.snapshot.sessions.contains(where: { $0.id == id && $0.attentionDismissVersion == version }) else {
                        return Self.json(["error": "artificial stale batch"], status: 409)
                    }
                    let old = current.snapshot
                    current.snapshot = Snapshot(demo: old.demo, now: old.now,
                        counts: Counts(active: 0, waiting: 0, error: 0, unknown: 0), childrenCounts: old.childrenCounts,
                        sessions: old.sessions.filter { $0.id != id }, attentionCounts: AttentionCounts(pending: 0),
                        recentCompleted: old.recentCompleted,
                        inboxCounts: InboxCounts(pending: 1, actionRequired: 0, unreadCompleted: 1))
                    return Self.json(["dismissed": true], delay: current.dismissDelay)
                }
                return Self.json(["error": "unexpected artificial action"], status: 400)
            }
            if path == "/api/snapshot" {
                current.snapshotRequests += 1
                if current.snapshotFailure { return Self.json(["error": "artificial snapshot failure"], status: 500) }
                let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
                let delay = current.snapshotRequests == 1 ? max(current.snapshotDelay, current.firstSnapshotDelay) : current.snapshotDelay
                return .response(200, ["Content-Type": "application/json"], try! encoder.encode(current.snapshot), delay)
            }
            if path == "/api/subscriptions" { return Self.json(["demo": current.snapshot.demo, "now": current.snapshot.now, "platforms": []]) }
            return Self.json(["error": "unexpected artificial request"], status: 404)
        }
    }
    private static func json(_ object: [String: Any], status: Int = 200, delay: TimeInterval = 0) -> ReadProtocol.Reply {
        .response(status, ["Content-Type": "application/json"], try! JSONSerialization.data(withJSONObject: object), delay)
    }
    private static func body(_ request: URLRequest) -> [String: String] {
        let data: Data
        if let body = request.httpBody { data = body }
        else if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var result = Data(), buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                guard count > 0 else { break }
                result.append(buffer, count: count)
            }
            data = result
        } else { return [:] }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: String] ?? [:]
    }
}

private final class ReadProtocol: URLProtocol {
    enum Reply {
        case response(Int, [String: String], Data, TimeInterval)
        case failure(URLError.Code)
    }
    private static let handler = ReadLock<((URLRequest) -> Reply)?>(nil)
    private let stopped = ReadLock(false)
    static func install(_ action: @escaping (URLRequest) -> Reply) { handler.modify { $0 = action } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handler.read() else { fatalError("Artificial transport missing") }
        let reply = handler(request)
        let delay: TimeInterval
        if case .response(_, _, _, let value) = reply { delay = value } else { delay = 0 }
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            guard !stopped.read() else { return }
            switch reply {
            case .response(let status, let headers, let data, _):
                let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            case .failure(let code): client?.urlProtocol(self, didFailWithError: URLError(code))
            }
        }
    }
    override func stopLoading() { stopped.modify { $0 = true } }
}
