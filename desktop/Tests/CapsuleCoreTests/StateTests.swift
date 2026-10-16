import Foundation
import Testing
@testable import CapsuleCore

@Suite("Capsule state mapping")
final class StateTests {
    let at: TimeInterval = 1_800_000_000
    let identity = QuotaIdentity(tool: "codex", bucket: "codex", key: "secondary")

    func counts(active: Int = 0, waiting: Int = 0, error: Int = 0, unknown: Int = 0, unknownRoles: Int? = 0) -> Counts {
        Counts(active: active, waiting: waiting, error: error, unknown: unknown, unknownRoles: unknownRoles)
    }
    func snapshot(main: Counts? = nil, child: Counts? = nil, includeChildren: Bool = true,
                  sessions: [SessionCard] = [], truncated: Bool = false,
                  attention: AttentionCounts? = AttentionCounts(pending: 0),
                  recentCompleted: [SessionCard]? = nil,
                  inbox: InboxCounts? = InboxCounts(pending: 0)) -> Snapshot {
        Snapshot(demo: true, now: at, counts: main ?? counts(), childrenCounts: includeChildren ? child ?? counts() : nil,
                 sessions: sessions, truncated: truncated, attentionCounts: attention, recentCompleted: recentCompleted,
                 inboxCounts: inbox)
    }
    func limit(remaining: Double? = 62, used: Double? = 38, cap: Double? = 100,
               status: String = "available", age: Double = 0, reset: Double? = nil,
               key: String = "secondary", bucket: String = "codex", minutes: Double? = 10080,
               unit: String = "percent", observed: Double? = nil) -> SubscriptionLimit {
        SubscriptionLimit(key: key, bucket: bucket, label: "每周额度", source: "codex_record",
                          observedAt: observed ?? at - age, unit: unit, status: status, limit: cap,
                          used: used, remaining: nil, remainingPercent: remaining,
                          windowMinutes: minutes, resetsAt: reset)
    }
    func subscriptions(_ limits: [SubscriptionLimit]? = nil, status: String = "available", tool: String = "codex") -> SubscriptionSnapshot {
        SubscriptionSnapshot(demo: true, now: at, platforms: [SubscriptionPlatform(tool: tool, name: tool, status: status, limits: limits ?? [limit()])])
    }
    func quota(_ source: SubscriptionSnapshot? = nil, failed: Bool = false, now: TimeInterval? = nil) -> QuotaPresentation {
        QuotaPresentation.evaluate(snapshot: source ?? subscriptions(), selection: identity, failed: failed, now: now ?? at)
    }

    @Test
    func testMainExecutionAndIndependentRootAttentionDoNotAddChildrenOrErrors() {
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 1, waiting: 2, error: 3), child: counts(active: 8, waiting: 9, error: 4),
                                                               attention: AttentionCounts(pending: 1, waitingUser: 1, waitingApproval: 1, error: 7),
                                                               inbox: InboxCounts(pending: 1, actionRequired: 1)), failed: false, age: 0)
        #expect(value.text == "1 执行 · 1 待处理")
        #expect(value.active == 1); #expect(value.pending == 1); #expect(value.fresh)
    }
    @Test
    func testTruncatedCardsNeverReplaceCompleteAggregates() {
        let card = SessionCard(id: "fake", tool: "future-tool", state: "idle", childrenSummary: counts(active: 500))
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 170), child: counts(active: 120), sessions: [card], truncated: true,
                                                               attention: AttentionCounts(pending: 165), inbox: InboxCounts(pending: 165, actionRequired: 165)), failed: false, age: 0)
        #expect(value.active == 170); #expect(value.pending == 165)
    }
    @Test
    func testKnownZeroHasNoCompactContent() {
        let value = CapsuleStatus.evaluate(snapshot: snapshot(), failed: false, age: 0)
        #expect(value.text.isEmpty); #expect(!value.hasContent); #expect(!(value.hasCoverageGap))
        #expect(value.active == 0); #expect(value.pending == 0)
    }
    @Test
    func testUnknownOnlyHasNoCompactContent() {
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(child: counts(unknown: 1)), failed: false, age: 0).text.isEmpty)
    }
    @Test
    func testUnknownRolesDoNotCreateCompactContent() {
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(main: counts(unknownRoles: 1)), failed: false, age: 0).text.isEmpty)
    }
    @Test
    func testMissingChildrenCountsDoesNotInferFromSummaries() {
        let card = SessionCard(id: "fake", tool: "codex", state: "idle", childrenSummary: counts(active: 100))
        let value = CapsuleStatus.evaluate(snapshot: snapshot(includeChildren: false, sessions: [card]), failed: false, age: 0)
        #expect(value.text.isEmpty); #expect(value.active == 0); #expect(value.hasCoverageGap)
    }
    @Test
    func testMissingUnknownRoleCoverageIsConservative() {
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(main: counts(unknownRoles: nil)), failed: false, age: 0).hasCoverageGap)
    }
    @Test
    func testKnownCountsRemainVisibleAlongsideCoverageGap() {
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2, unknown: 3)), failed: false, age: 0)
        #expect(value.text == "2 执行"); #expect(value.hasCoverageGap)
    }
    @Test
    func testConnectingDoesNotInventZeros() {
        let value = CapsuleStatus.evaluate(snapshot: nil, failed: false, age: nil)
        #expect(value.text.isEmpty); #expect(!value.hasContent); #expect(value.active == nil); #expect(value.pending == nil)
    }
    @Test
    func testFailureAndSuspensionInvalidateFreshnessImmediately() {
        for value in [CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2)), failed: true, age: 0),
                      CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2)), failed: false, age: 0, suspended: true)] {
            #expect(value.text.isEmpty); #expect(!value.hasContent); #expect(!(value.fresh)); #expect(value.active == nil)
        }
    }
    @Test
    func testFreshnessDeadlineUsesAgeAndFailsClosedForMissingOrNonfiniteAge() {
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(), failed: false, age: 14.99).fresh)
        for age in [TimeInterval?(15), 20, nil, .nan, .infinity, -1] {
            #expect(!(CapsuleStatus.evaluate(snapshot: snapshot(), failed: false, age: age).fresh))
        }
    }
    @Test
    func testNegativeCountsAreInvalidAndMainCountNeverAddsAChildOverflow() {
        #expect(!(CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: -1)), failed: false, age: 0).fresh))
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: Int.max), child: counts(active: 1)), failed: false, age: 0).active == Int.max)
        let invalidInbox = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 1), inbox: InboxCounts(pending: -1)), failed: false, age: 0)
        #expect(invalidInbox.pending == nil); #expect(invalidInbox.hasCoverageGap)
    }
    @Test
    func testMissingInboxIsUnknownAndNeverInferredFromAttentionOrTruncatedCards() {
        let card = SessionCard(id: "fake", tool: "codex", state: "waiting_user", role: "main",
                               attention: SessionAttention(pending: true, waitingUser: 1))
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2, waiting: 120, error: 8), sessions: [card], truncated: true,
                                                               attention: AttentionCounts(pending: 120), inbox: nil), failed: false, age: 0)
        #expect(value.pending == nil); #expect(value.active == 2); #expect(value.hasCoverageGap)
        #expect(value.text == "2 执行")
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(inbox: nil), failed: false, age: 0).text.isEmpty)
    }
    @Test
    func testUncertainHumanRequirementCannotBecomeZeroPendingAlongsideExecution() {
        let uncertain = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2), inbox: InboxCounts(pending: 0, unknown: 1)), failed: false, age: 0)
        #expect(uncertain.active == 2); #expect(uncertain.pending == nil); #expect(uncertain.hasCoverageGap)
        #expect(uncertain.text == "2 执行")
        #expect(CapsuleStatus.evaluate(snapshot: snapshot(inbox: InboxCounts(pending: 0, unknown: 1)), failed: false, age: 0).text.isEmpty)
        let confirmedSome = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2), inbox: InboxCounts(pending: 1, unknown: 2)), failed: false, age: 0)
        #expect(confirmedSome.pending == 1); #expect(confirmedSome.hasCoverageGap)
        #expect(confirmedSome.text == "2 执行 · 1 待处理")
        let stateOnlyGap = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2, unknown: 3), child: counts(unknown: 4), attention: AttentionCounts(pending: 0)), failed: false, age: 0)
        #expect(stateOnlyGap.pending == 0); #expect(stateOnlyGap.hasCoverageGap)
        #expect(stateOnlyGap.text == "2 执行")
    }
    @Test
    func testPendingOnlyIsVisibleWithoutZeroExecutionOrCoverageText() {
        for unknown in [0, 2] {
            let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(unknown: unknown),
                inbox: InboxCounts(pending: 3, actionRequired: 1, unreadCompleted: 2, unknown: unknown)), failed: false, age: 0)
            #expect(value.text == "3 待处理"); #expect(value.hasContent)
            #expect(value.active == 0); #expect(value.pending == 3)
            #expect(value.hasCoverageGap == (unknown > 0))
        }
    }
    @Test
    func testUnknownPendingRemainsNilWhenCompactContentIsEmpty() {
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(unknown: 2),
            inbox: InboxCounts(pending: 0, unknown: 1)), failed: false, age: 0)
        #expect(value.text.isEmpty); #expect(!value.hasContent)
        #expect(value.active == 0); #expect(value.pending == nil); #expect(value.hasCoverageGap)
    }
    @Test
    func testExpiredCountsCannotKeepCompactContentVisible() {
        for age in [TimeInterval?(15), 20, nil, .nan, .infinity, -1] {
            let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2),
                inbox: InboxCounts(pending: 3)), failed: false, age: age)
            #expect(value.text.isEmpty); #expect(!value.hasContent)
            #expect(value.active == nil); #expect(value.pending == nil); #expect(!value.fresh)
        }
    }
    @Test
    func testErrorAndAutomaticApprovalDoNotInventHumanAttention() {
        for state in ["waiting_approval", "error"] {
            let card = SessionCard(id: state, tool: "codex", state: state, role: "main",
                                   attention: SessionAttention(pending: false, error: state == "error" ? 1 : 0))
            #expect(card.requiresHumanAttention == false)
        }
        #expect(SessionCard(id: "legacy", tool: "codex", state: "waiting_user").requiresHumanAttention == nil)
        #expect(SessionCard(id: "error", tool: "codex", state: "error").requiresHumanAttention == nil)
    }
    @Test
    func testAttentionDismissVersionDecodesAsAnOpaqueBatch() throws {
        let version = String(repeating: "a1", count: 32)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let json = "{\"pending\":true,\"waiting_user\":2,\"waiting_approval\":0,\"error\":0,\"unknown\":0,\"dismiss_version\":\"\(version)\"}"
        let attention = try decoder.decode(SessionAttention.self, from: Data(json.utf8))
        let card = SessionCard(id: "artificial", tool: "codex", state: "waiting_user", role: "main", attention: attention)
        #expect(card.attentionDismissVersion == version)
        #expect(card.requiresHumanAttention == true)
    }
    @Test
    func testManualAttentionConfirmationRequiresExplicitRootAndCurrentVersion() {
        let version = String(repeating: "f", count: 64)
        for role in [String?(nil), "unknown", "child"] {
            let card = SessionCard(id: "artificial", tool: "codex", state: "waiting_user", role: role,
                                   attention: SessionAttention(pending: true, waitingUser: 1, dismissVersion: version))
            #expect(card.attentionDismissVersion == nil)
        }
        let inactive = SessionCard(id: "artificial", tool: "codex", state: "waiting_approval", role: "main",
                                   attention: SessionAttention(pending: false, dismissVersion: version))
        #expect(inactive.attentionDismissVersion == nil)
        let unknown = SessionCard(id: "artificial", tool: "codex", state: "waiting_user", role: "main")
        #expect(unknown.attentionDismissVersion == nil)
    }
    @Test
    func testMissingOrMalformedDismissVersionKeepsHumanPendingWithoutAnAction() {
        for version in [String?(nil), "", String(repeating: "a", count: 63), String(repeating: "a", count: 65),
                        String(repeating: "g", count: 64), String(repeating: "A", count: 64), String(repeating: "é", count: 32)] {
            let card = SessionCard(id: "artificial", tool: "codex", state: "waiting_user", role: "main",
                                   attention: SessionAttention(pending: true, waitingUser: 1, dismissVersion: version))
            #expect(card.attentionDismissVersion == nil)
            #expect(card.requiresHumanAttention == true)
        }
    }
    @Test
    func testAttentionAndExecutionCanCoexistWithoutDuplicatingDetailRows() {
        let both = SessionCard(id: "both", tool: "codex", state: "active", role: "main", stateSource: "hook", liveness: "unknown",
                               attention: SessionAttention(pending: true, waitingUser: 2, waitingApproval: 1))
        let active = SessionCard(id: "active", tool: "codex", state: "active", role: "main", stateSource: "hook", liveness: "online",
                                 attention: SessionAttention(pending: false))
        let child = SessionCard(id: "child", tool: "codex", state: "waiting_user", role: "child", attention: SessionAttention(pending: true, waitingUser: 1))
        let unknownRole = SessionCard(id: "unknown", tool: "codex", state: "waiting_user", role: "unknown", attention: SessionAttention(pending: true, waitingUser: 1))
        let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot(sessions: [both, active, both, child, unknownRole], attention: AttentionCounts(pending: 1)), now: at)
        #expect(groups.pending.map(\.id) == ["both"]); #expect(groups.active.map(\.id) == ["active"])
        #expect(groups.pending[0].confirmedActive)
        #expect(groups.pending[0].attentionLabels == ["待回复", "待批准"])
    }
    @Test
    func testRecentCompletionIsIndependentSortedLimitedAndNeverInferredFromIdle() {
        let recent = (0..<8).map { i in
            SessionCard(id: "done-\(i)", tool: "codex", state: "idle", role: "main",
                        attention: SessionAttention(pending: false), lastCompletedAt: at - Double(i * 60),
                        inbox: SessionInbox(unreadCompleted: true, completedAt: at - Double(i * 60)))
        }
        let ordinaryIdle = SessionCard(id: "idle", tool: "claude", state: "idle", role: "main", attention: SessionAttention(pending: false))
        let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot(sessions: [ordinaryIdle], truncated: true, recentCompleted: Array(recent.reversed())), now: at)
        #expect(groups.completed.map(\.id) == ["done-0", "done-1", "done-2", "done-3", "done-4"])
        #expect(groups.isTruncated); #expect(groups.hasCompletionCoverage)
        let legacy = CapsuleDetailGroups.evaluate(snapshot: snapshot(sessions: recent), now: at)
        #expect(legacy.completed.isEmpty); #expect(!legacy.hasCompletionCoverage)
    }
    @Test
    func testRecentCompletionRejectsOldFutureUnknownDuplicateAndCurrentlyBusyCards() {
        func done(_ id: String, time: Double, role: String = "main", attention: SessionAttention? = SessionAttention(pending: false)) -> SessionCard {
            SessionCard(id: id, tool: "codex", state: "idle", role: role, attention: attention, lastCompletedAt: time,
                        inbox: SessionInbox(unreadCompleted: true, completedAt: time))
        }
        let active = SessionCard(id: "active", tool: "codex", state: "active", role: "main", stateSource: "hook", liveness: "online", attention: SessionAttention(pending: false))
        let pending = SessionCard(id: "pending", tool: "codex", state: "waiting_user", role: "main", attention: SessionAttention(pending: true, waitingUser: 1))
        let recent = [done("boundary", time: at - 86_400), done("old", time: at - 86_401), done("future", time: at + 1),
                      done("active", time: at - 5), done("pending", time: at - 5), done("child", time: at - 5, role: "child"),
                      done("missing-attention", time: at - 5, attention: nil), done("boundary", time: at - 86_400)]
        let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot(sessions: [active, pending], recentCompleted: recent), now: at)
        #expect(groups.completed.map(\.id) == ["boundary"])
        #expect(CompletionRecency.label(at: at, now: at) == "刚完成")
        #expect(CompletionRecency.label(at: at - 60, now: at) == "1 分钟前")
        #expect(CompletionRecency.label(at: at - 3_600, now: at) == "1 小时前")
        #expect(CompletionRecency.label(at: at - 86_401, now: at) == nil)
        #expect(CompletionRecency.label(at: nil, now: at) == nil)
    }
    @Test
    func testRecentCompletionRequiresSettledKnownStateBeforeApplyingFiveRowLimit() {
        func completed(_ id: String, offset: Double) -> SessionCard {
            SessionCard(id: id, tool: "codex", state: "idle", role: "main",
                        attention: SessionAttention(pending: false), lastCompletedAt: at - offset,
                        inbox: SessionInbox(unreadCompleted: true, completedAt: at - offset))
        }
        let unknownRound = SessionCard(id: "new-unknown-round", tool: "codex", state: "unknown", role: "main",
                                       attention: SessionAttention(pending: false))
        let errorRound = SessionCard(id: "new-error-round", tool: "codex", state: "error", role: "main",
                                     attention: SessionAttention(pending: false, error: 1))
        let unknownAttention = SessionCard(id: "unknown-attention", tool: "codex", state: "idle", role: "main",
                                           attention: SessionAttention(pending: false, unknown: 1))
        let valid = (0..<5).map { completed("valid-\($0)", offset: 60 + Double($0)) }
        let misleadingRecent = [completed(unknownRound.id, offset: 1), completed(errorRound.id, offset: 2),
                                completed(unknownAttention.id, offset: 3)] + valid
        let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot(sessions: [unknownRound, errorRound, unknownAttention], recentCompleted: misleadingRecent), now: at)
        #expect(groups.completed.map(\.id) == valid.map(\.id))
        #expect(groups.completed.count == 5)
        let unsettledSource = SessionCard(id: "new-unsettled", tool: "codex", state: "unknown", role: "main",
                                          attention: SessionAttention(pending: false), lastCompletedAt: at - 1,
                                          inbox: SessionInbox(unreadCompleted: true, completedAt: at - 1))
        #expect(CapsuleDetailGroups.evaluate(snapshot: snapshot(recentCompleted: [unsettledSource]), now: at).completed.isEmpty)
    }
    @Test
    func testEventCanConfirmActiveWithUnknownProcessButOfflineCannot() {
        #expect(SessionCard(id: "a", tool: "codex", state: "active", stateSource: "event", liveness: "unknown").confirmedActive)
        let offline = SessionCard(id: "b", tool: "codex", state: "active", stateSource: "hook", liveness: "offline")
        #expect(!(offline.confirmedActive)); #expect(offline.stateLabel == "执行待确认")
    }
    @Test
    func testProcessOnlineAloneDoesNotMeanExecuting() {
        #expect(!(SessionCard(id: "a", tool: "claude", state: "idle", liveness: "online").confirmedActive))
        #expect(!(SessionCard(id: "a", tool: "claude", state: "active", stateSource: "record", liveness: "unknown").confirmedActive))
    }
    @Test
    func testOldActiveAndOldWaitingKeepDifferentMeanings() {
        #expect(!(SessionCard(id: "a", tool: "codex", state: "active", stale: true, stateSource: "hook", liveness: "online").confirmedActive))
        #expect(!(SessionCard(id: "a", tool: "codex", state: "active", stateSource: "hook", stateAge: 121, liveness: "online").confirmedActive))
        #expect(SessionCard(id: "a", tool: "claude", state: "waiting_user", stale: true).stateLabel == "待回复")
    }
    @Test
    func testDisplayNameAndUnknownToolStateRetainSafeFallbacks() {
        #expect(SessionCard(id: "a", tool: "new-tool", state: "future", alias: "  ", projectName: " 项目 ").displayName == "项目")
        #expect(SessionCard(id: "a", tool: "new-tool", state: "future").stateLabel == "未知状态（future）")
        for project in [String?(nil), "", " \n ", " 未识别项目 ", "未知项目"] {
            #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: " 会话别名 ", sessionTitle: "原标题", projectName: project).displayName == "会话别名")
            #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: " \n ", sessionTitle: " 原标题 ", projectName: project).displayName == "原标题")
            #expect(SessionCard(id: "a", tool: "codex", state: "idle", projectName: project).displayName == "未命名会话")
        }
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "真实会话", projectName: "占位名称", projectId: " unknown ").displayName == "真实会话")
        let internalName = "g-p-" + String(repeating: "a", count: 32)
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "真实会话", projectName: internalName,
                            workingDirectory: "/artificial/.codex/.chatgpt-projects/" + internalName).displayName == "真实会话")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "真实会话", projectName: internalName,
                            workingDirectory: "~/.codex/.chatgpt-projects/" + internalName).displayName == "真实会话")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", projectName: internalName,
                            workingDirectory: "~/.codex/.chatgpt-projects/" + internalName).displayName == "未命名会话")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", projectName: internalName,
                            workingDirectory: "~/.codex/ordinary-projects/" + internalName).displayName == internalName)
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: "会话别名", sessionTitle: "原标题", projectName: internalName,
                            workingDirectory: "/artificial/.codex/.chatgpt-projects/" + internalName + "/work").displayName == "会话别名")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", projectName: internalName,
                            workingDirectory: "/artificial/projects/" + internalName).displayName == internalName)
        for project in ["g-p-demo-title-project", "users-artificial-project", "2026", "6a4ae1c0-3a0d-4000-a000-000000000000"] {
            #expect(SessionCard(id: "a", tool: "codex", state: "idle", projectName: project,
                                workingDirectory: "/artificial/.chatgpt-projects/" + project).displayName == project)
            #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "正式侧栏会话", projectName: project,
                                workingDirectory: "/artificial/.chatgpt-projects/" + project).displayName == "正式侧栏会话")
        }
        for tool in ["claude", "workbuddy", "future-tool"] {
            #expect(SessionCard(id: "a", tool: tool, state: "idle", alias: "会话别名", sessionTitle: "会话标题", projectName: "原有项目").displayName == "原有项目")
        }
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: "人工会话名", sessionTitle: "侧栏会话", projectName: "users-artificial-output").displayName == "人工会话名")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "侧栏会话", projectName: "[https://example.invalid/artifact]").displayName == "侧栏会话")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: "会话", projectDisplayName: " 正式项目 ", projectName: "目录", projectId: "unknown").displayName == "正式项目")
    }
    @Test
    func testOfficialProjectOrSessionNamesRemainCompleteWithoutCharacterLimits() throws {
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: " 别名 ", sessionTitle: " 会话 ", projectDisplayName: " 项目 ", projectName: "目录").displayName == "项目")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", alias: " ", sessionTitle: " 会话 ", projectDisplayName: " 项目 ", projectName: "目录").displayName == "项目")
        #expect(SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: " \n ", projectName: " 项目 ").displayName == "项目")
        for length in [9, 10, 11, 49, 50, 51, 160, 256] {
            let name = String(repeating: "项", count: length)
            let card = SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: "会话", projectDisplayName: name, projectName: "输出目录")
            #expect(card.displayName == name)
            #expect(card.displayName.count == length)
        }
        let emoji = "👩🏽‍💻"
        #expect(emoji.count == 1)
        let longSession = String(repeating: emoji, count: 51)
        let session = SessionCard(id: "a", tool: "codex", state: "idle", alias: longSession, sessionTitle: "原标题", projectName: "未识别项目")
        #expect(session.displayName == longSession)
        #expect(session.displayName.count == 51)
        let combined = String(repeating: "👨‍👩‍👧‍👦e\u{301}", count: 60)
        let titleOnly = SessionCard(id: "a", tool: "codex", state: "idle", sessionTitle: combined)
        #expect(titleOnly.displayName == combined && titleOnly.displayName.count == 120)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(SessionCard.self, from: Data(#"{"id":"artificial","tool":"codex","state":"idle","stale":false,"session_title":"人工会话标题"}"#.utf8))
        #expect(value.sessionTitle == "人工会话标题")
        #expect(value.projectDisplayName == nil)
        #expect(value.displayName == "人工会话标题")
        #expect(value.replacingInbox(SessionInbox(unreadCompleted: false)).sessionTitle == value.sessionTitle)
        #expect(value.replacingAttention(SessionAttention(pending: false)).sessionTitle == value.sessionTitle)
        let official = try decoder.decode(SessionCard.self, from: Data(#"{"id":"artificial","tool":"codex","state":"idle","stale":false,"project_display_name":"正式人工项目","session_title":"正式会话标题","project_name":"users-artificial-output"}"#.utf8))
        #expect(official.projectDisplayName == "正式人工项目" && official.displayName == "正式人工项目")
        #expect(official.replacingInbox(SessionInbox(unreadCompleted: false)).projectDisplayName == official.projectDisplayName)
        #expect(official.replacingAttention(SessionAttention(pending: false)).projectDisplayName == official.projectDisplayName)
        #expect(session.replacingInbox(SessionInbox(unreadCompleted: false)).displayName == longSession)
        #expect(titleOnly.replacingAttention(SessionAttention(pending: false)).displayName == combined)
    }
    @Test
    func testSnakeCaseDecodingPreservesParentIdAndOptionalNulls() throws {
        let raw = #"{"demo":true,"now":1800000000,"counts":{"active":1,"waiting":0,"error":0,"unknown":0,"unknown_roles":0},"children_counts":null,"sessions":[{"id":"x","tool":"future","state":"waiting_user","stale":true,"parent_id":"p","project_name":"示例","working_directory":null,"jump":{"available":false,"label":"不可跳转","precision":null,"kind":"none"}}],"truncated":false}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(Snapshot.self, from: Data(raw.utf8))
        #expect(value.counts.unknownRoles == 0); #expect(value.childrenCounts == nil)
        #expect(value.sessions[0].parentId == "p"); #expect(value.sessions[0].workingDirectory == nil)
    }
    @Test
    func testMissingRequiredCounterFailsDecodingInsteadOfBecomingZero() {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        #expect(throws: DecodingError.self) { try decoder.decode(Counts.self, from: Data(#"{"waiting":0,"error":0,"unknown":0}"#.utf8)) }
    }
    @Test
    func testAttentionContractDecodesIndependentRootCountsAndReliableCompletion() throws {
        let raw = #"{"demo":true,"now":1800000000,"counts":{"active":1,"waiting":3,"error":4,"unknown":0,"unknown_roles":0},"children_counts":null,"attention_counts":{"pending":1,"waiting_user":1,"waiting_approval":1,"error":4,"unknown":0},"inbox_counts":{"pending":2,"action_required":1,"unread_completed":1,"unknown":0},"sessions":[{"id":"root","tool":"codex","state":"active","stale":false,"role":"main","attention":{"pending":true,"waiting_user":2,"waiting_approval":1,"error":0,"unknown":0,"oldest_at":1799999950},"last_completed_at":null,"inbox":{"unread_completed":false,"completed_at":null}}],"recent_completed":[{"id":"done","tool":"claude","state":"idle","stale":false,"role":"main","attention":{"pending":false,"waiting_user":0,"waiting_approval":0,"error":0,"unknown":0,"oldest_at":null},"last_completed_at":1799999990,"inbox":{"unread_completed":true,"completed_at":1799999990}}],"truncated":true}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(Snapshot.self, from: Data(raw.utf8))
        #expect(value.attentionCounts?.pending == 1)
        #expect(value.inboxCounts == InboxCounts(pending: 2, actionRequired: 1, unreadCompleted: 1))
        #expect(value.sessions[0].attention?.waitingUser == 2)
        #expect(value.sessions[0].lastCompletedAt == nil)
        #expect(value.recentCompleted?[0].lastCompletedAt == at - 10)
        #expect(value.recentCompleted?[0].inbox?.completedAt == at - 10)
        #expect(value.recentCompleted?[0].unreadCompleted == true)
        #expect(CapsuleStatus.evaluate(snapshot: value, failed: false, age: 0).pending == 2)
        #expect(CapsuleDetailGroups.evaluate(snapshot: value, now: at).completed.map(\.id) == ["done"])
        let legacyRaw = #"{"demo":true,"now":1800000000,"counts":{"active":0,"waiting":0,"error":0,"unknown":0},"sessions":[],"truncated":false}"#
        let legacy = try decoder.decode(Snapshot.self, from: Data(legacyRaw.utf8))
        #expect(legacy.attentionCounts == nil); #expect(legacy.recentCompleted == nil)
        #expect(CapsuleStatus.evaluate(snapshot: legacy, failed: false, age: 0).pending == nil)
        #expect(throws: DecodingError.self) {
            try decoder.decode(AttentionCounts.self, from: Data(#"{"waiting_user":0,"waiting_approval":0,"error":0,"unknown":0}"#.utf8))
        }
        #expect(throws: DecodingError.self) {
            try decoder.decode(InboxCounts.self, from: Data(#"{"action_required":0,"unread_completed":0,"unknown":0}"#.utf8))
        }
        #expect(throws: DecodingError.self) {
            try decoder.decode(SessionInbox.self, from: Data(#"{"completed_at":null}"#.utf8))
        }
    }
    @Test
    func testInboxRootUnionIncludesUnreadCompletionWithoutAddingSubcounts() {
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 1), attention: AttentionCounts(pending: 2),
                                                               inbox: InboxCounts(pending: 3, actionRequired: 2, unreadCompleted: 1)), failed: false, age: 0)
        #expect(value.text == "1 执行 · 3 待处理")
        let overlap = CapsuleStatus.evaluate(snapshot: snapshot(attention: AttentionCounts(pending: 1),
                                                                 inbox: InboxCounts(pending: 1, actionRequired: 1, unreadCompleted: 1)), failed: false, age: 0)
        #expect(overlap.pending == 1)
    }
    @Test
    func testCompletionListRequiresUnreadCoverageAndMatchingVersion() {
        func done(_ id: String, inbox: SessionInbox?) -> SessionCard {
            SessionCard(id: id, tool: "codex", state: "idle", role: "main", attention: SessionAttention(pending: false),
                        lastCompletedAt: at - 10, inbox: inbox)
        }
        let rows = [done("unread", inbox: SessionInbox(unreadCompleted: true, completedAt: at - 10)),
                    done("read", inbox: SessionInbox(unreadCompleted: false, completedAt: at - 10)),
                    done("missing", inbox: nil), done("mismatch", inbox: SessionInbox(unreadCompleted: true, completedAt: at - 20))]
        let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot(recentCompleted: rows), now: at)
        #expect(groups.completed.map(\.id) == ["unread"])
        #expect(!groups.hasCompletionCoverage)
        #expect(rows[2].unreadCompleted == nil); #expect(rows[3].unreadCompleted == nil)
    }
    @Test
    func testReadProjectionRemovesOnlyKnownCompletionAndDoesNotRecalculateTruncatedTotals() {
        let root = SessionCard(id: "done", tool: "codex", state: "idle", role: "main", projectDisplayName: "正式人工项目", attention: SessionAttention(pending: false),
                               lastCompletedAt: at - 10, inbox: SessionInbox(unreadCompleted: true, completedAt: at - 10))
        let original = snapshot(sessions: [root], truncated: true, attention: AttentionCounts(pending: 40), recentCompleted: [root],
                                inbox: InboxCounts(pending: 70, actionRequired: 40, unreadCompleted: 30))
        let read = original.applyingCompletionRead(id: root.id, through: at - 10)
        #expect(read.inboxCounts == InboxCounts(pending: 69, actionRequired: 40, unreadCompleted: 29))
        #expect(read.sessions[0].unreadCompleted == false); #expect(read.recentCompleted?.isEmpty == true)
        #expect(read.sessions[0].projectDisplayName == "正式人工项目")
        #expect(read.attentionCounts == original.attentionCounts); #expect(read.counts == original.counts); #expect(read.truncated)
        #expect(read.applyingCompletionRead(id: root.id, through: at - 10) == read)
        #expect(original.applyingCompletionRead(id: "not-known", through: at) == original)
        #expect(original.applyingCompletionRead(id: root.id, through: .nan) == original)
        let offList = snapshot(sessions: [], truncated: true, recentCompleted: [root], inbox: InboxCounts(pending: 1, unreadCompleted: 1))
        #expect(offList.applyingCompletionRead(id: root.id, through: at).inboxCounts?.pending == 0)
    }
    @Test
    func testReadProjectionCannotClearNewerCompletionOrHumanRequestAndKeepsLowerBounds() {
        func root(_ pending: Bool, version: Double) -> SessionCard {
            SessionCard(id: "same-root", tool: "codex", state: "idle", role: "main",
                        attention: SessionAttention(pending: pending, waitingUser: pending ? 1 : 0), lastCompletedAt: version,
                        inbox: SessionInbox(unreadCompleted: true, completedAt: version))
        }
        let newer = root(false, version: at)
        let old = root(false, version: at - 10)
        let newSnapshot = snapshot(sessions: [newer], recentCompleted: [old], inbox: InboxCounts(pending: 1, unreadCompleted: 1))
        #expect(newSnapshot.applyingCompletionRead(id: newer.id, through: at - 10) == newSnapshot)
        let reappeared = snapshot(sessions: [newer], recentCompleted: [newer], inbox: InboxCounts(pending: 1, unreadCompleted: 1))
        #expect(CapsuleDetailGroups.evaluate(snapshot: reappeared, now: at).completed.map(\.id) == [newer.id])
        let human = root(true, version: at - 10)
        let humanSnapshot = snapshot(sessions: [human], attention: AttentionCounts(pending: 1, waitingUser: 1), recentCompleted: [human],
                                     inbox: InboxCounts(pending: 1, actionRequired: 1, unreadCompleted: 1))
        let read = humanSnapshot.applyingCompletionRead(id: human.id, through: at - 10)
        #expect(read.inboxCounts == InboxCounts(pending: 1, actionRequired: 1, unreadCompleted: 0))
        #expect(read.sessions[0].requiresHumanAttention == true); #expect(read.attentionCounts == humanSnapshot.attentionCounts)
        let emptyCounts = snapshot(sessions: [old], recentCompleted: [old], inbox: InboxCounts(pending: 0, unreadCompleted: 0))
        #expect(emptyCounts.applyingCompletionRead(id: old.id, through: at).inboxCounts == emptyCounts.inboxCounts)
        let missingCounts = snapshot(sessions: [old], recentCompleted: [old], inbox: nil)
        #expect(missingCounts.applyingCompletionRead(id: old.id, through: at).inboxCounts == nil)
        let malformedCounts = snapshot(sessions: [old], recentCompleted: [old], inbox: InboxCounts(pending: 1, actionRequired: 2, unreadCompleted: 1))
        #expect(malformedCounts.applyingCompletionRead(id: old.id, through: at).inboxCounts == malformedCounts.inboxCounts)
        let unknownHuman = SessionCard(id: old.id, tool: "codex", state: "idle", role: "main", lastCompletedAt: at - 10,
                                       inbox: SessionInbox(unreadCompleted: true, completedAt: at - 10))
        let unknownSnapshot = snapshot(sessions: [unknownHuman], recentCompleted: [unknownHuman], inbox: InboxCounts(pending: 1, unreadCompleted: 1))
        #expect(unknownSnapshot.applyingCompletionRead(id: old.id, through: at).inboxCounts == unknownSnapshot.inboxCounts)
    }
    @Test
    func testAttentionDismissProjectionImmediatelyUpdatesExactRootWithoutRecountingTruncatedCards() {
        let version = String(repeating: "a", count: 64)
        let root = SessionCard(id: "root", tool: "codex", state: "active", role: "main", sessionTitle: "人工并发会话", projectDisplayName: "正式人工项目",
            stateAt: at, stateSource: "hook", liveness: "online",
            attention: SessionAttention(pending: true, waitingUser: 4, waitingApproval: 2, error: 1, unknown: 3,
                                        oldestAt: at - 120, dismissVersion: version),
            inbox: SessionInbox(unreadCompleted: false))
        let child = SessionCard(id: "child", tool: "codex", state: "waiting_user", role: "child", parentId: root.id,
            attention: SessionAttention(pending: true, waitingUser: 1))
        let original = snapshot(main: counts(active: 30, waiting: 14), sessions: [root, child], truncated: true,
            attention: AttentionCounts(pending: 40, waitingUser: 35, waitingApproval: 9, error: 6, unknown: 8),
            inbox: InboxCounts(pending: 70, actionRequired: 40, unreadCompleted: 30, unknown: 8))
        let value = original.applyingAttentionDismiss(id: root.id, version: version)
        #expect(value.attentionCounts == AttentionCounts(pending: 39, waitingUser: 34, waitingApproval: 8, error: 6, unknown: 8))
        #expect(value.inboxCounts == InboxCounts(pending: 69, actionRequired: 39, unreadCompleted: 30, unknown: 8))
        #expect(value.sessions[0].requiresHumanAttention == false)
        #expect(value.sessions[0].attention == SessionAttention(pending: false, error: 1, unknown: 3, oldestAt: at - 120))
        #expect(value.sessions[0].attentionDismissVersion == nil)
        #expect(value.sessions[0].confirmedActive && value.sessions[0].sessionTitle == "人工并发会话")
        #expect(value.sessions[0].projectDisplayName == "正式人工项目")
        #expect(value.sessions[0].inbox == root.inbox && value.sessions[1] == child)
        #expect(value.counts == original.counts && value.childrenCounts == original.childrenCounts && value.truncated)
        #expect(value.applyingAttentionDismiss(id: root.id, version: version) == value)
        #expect(CapsuleDetailGroups.evaluate(snapshot: value, now: at).pending.isEmpty)
        #expect(CapsuleDetailGroups.evaluate(snapshot: value, now: at).active.map(\.id) == [root.id])
    }
    @Test
    func testAttentionDismissProjectionNeverClearsDifferentOrUnknownBatch() {
        let a = String(repeating: "a", count: 64), b = String(repeating: "b", count: 64)
        func root(_ version: String?, role: String = "main") -> SessionCard {
            SessionCard(id: "root", tool: "codex", state: "waiting_user", role: role,
                attention: SessionAttention(pending: true, waitingUser: 1, dismissVersion: version),
                inbox: SessionInbox(unreadCompleted: false))
        }
        for card in [root(b), root(nil), root("invalid"), root(a, role: "unknown"), root(a, role: "child")] {
            let original = snapshot(sessions: [card], attention: AttentionCounts(pending: 1, waitingUser: 1), inbox: InboxCounts(pending: 1, actionRequired: 1))
            #expect(original.applyingAttentionDismiss(id: card.id, version: a) == original)
        }
        let missing = snapshot(sessions: [], attention: AttentionCounts(pending: 10), inbox: InboxCounts(pending: 10, actionRequired: 10))
        #expect(missing.applyingAttentionDismiss(id: "root", version: a) == missing)
        let conflicting = snapshot(sessions: [root(a), root(b)], attention: AttentionCounts(pending: 1, waitingUser: 1), inbox: InboxCounts(pending: 1, actionRequired: 1))
        #expect(conflicting.applyingAttentionDismiss(id: "root", version: a) == conflicting)
    }
    @Test
    func testAttentionDismissProjectionPreservesUnreadCompletionUnionAndUnknownCoverage() {
        let version = String(repeating: "a", count: 64)
        func root(_ inbox: SessionInbox?) -> SessionCard {
            SessionCard(id: "root", tool: "codex", state: "idle", role: "main", sessionTitle: "人工标题", projectDisplayName: "正式人工项目",
                attention: SessionAttention(pending: true, waitingUser: 2, unknown: 2, dismissVersion: version),
                lastCompletedAt: at - 10, inbox: inbox)
        }
        let card = root(SessionInbox(unreadCompleted: true, completedAt: at - 10))
        let overlap = snapshot(sessions: [card], attention: AttentionCounts(pending: 1, waitingUser: 1, unknown: 1), recentCompleted: [card],
            inbox: InboxCounts(pending: 1, actionRequired: 1, unreadCompleted: 1, unknown: 1))
        let value = overlap.applyingAttentionDismiss(id: card.id, version: version)
        #expect(value.inboxCounts == InboxCounts(pending: 1, actionRequired: 0, unreadCompleted: 1, unknown: 1))
        #expect(value.sessions[0].unreadCompleted == true && value.recentCompleted?[0].unreadCompleted == true)
        #expect(value.recentCompleted?[0].sessionTitle == "人工标题")
        #expect(value.recentCompleted?[0].projectDisplayName == "正式人工项目")
        #expect(value.attentionCounts?.unknown == 1 && value.sessions[0].attention?.unknown == 2)
        let missing = snapshot(sessions: [root(nil)], attention: AttentionCounts(pending: 1, waitingUser: 1, unknown: 1),
            inbox: InboxCounts(pending: 1, actionRequired: 1, unknown: 1))
        #expect(missing.applyingAttentionDismiss(id: "root", version: version).inboxCounts == InboxCounts(pending: 1, actionRequired: 0, unknown: 1))
        let unknown = snapshot(sessions: [root(SessionInbox(unreadCompleted: false))], attention: AttentionCounts(pending: 1, waitingUser: 1, unknown: 1),
            inbox: InboxCounts(pending: 1, actionRequired: 1, unknown: 1)).applyingAttentionDismiss(id: "root", version: version)
        #expect(unknown.inboxCounts?.pending == 0 && unknown.inboxCounts?.unknown == 1)
        #expect(CapsuleStatus.evaluate(snapshot: unknown, failed: false, age: 0).pending == nil)
    }
    @Test
    func testAttentionDismissProjectionDoesNotInventMissingOrInconsistentAggregates() {
        let version = String(repeating: "a", count: 64)
        let card = SessionCard(id: "root", tool: "codex", state: "waiting_user", role: "main",
            attention: SessionAttention(pending: true, waitingUser: 1, dismissVersion: version), inbox: SessionInbox(unreadCompleted: false))
        for inbox in [InboxCounts?(nil), InboxCounts(pending: -1, actionRequired: 1), InboxCounts(pending: 1, actionRequired: 2)] {
            let original = snapshot(sessions: [card], attention: nil, inbox: inbox)
            let value = original.applyingAttentionDismiss(id: card.id, version: version)
            #expect(value.attentionCounts == nil && value.inboxCounts == inbox)
            #expect(value.sessions[0].requiresHumanAttention == false)
        }
        let invalid = snapshot(sessions: [card], attention: AttentionCounts(pending: 1, waitingUser: 0), inbox: InboxCounts(pending: 0))
        let value = invalid.applyingAttentionDismiss(id: card.id, version: version)
        #expect(value.attentionCounts == invalid.attentionCounts && value.inboxCounts == invalid.inboxCounts)
    }
    @Test
    func testQuotaZeroOneThirtyEightAndHundredAreLiteralFractions() {
        for used in [0.0, 1, 38, 100] {
            let value = quota(subscriptions([limit(remaining: 100 - used)]))
            #expect(abs((value.fraction!) - (used / 100)) <= 1e-12); #expect(value.freshness == .valid)
        }
    }
    @Test
    func testRemainingPercentWinsOverConflictingOldUsedField() {
        #expect(abs((quota(subscriptions([limit(remaining: 90, used: 99)])).fraction!) - (0.1)) <= 1e-12)
    }
    @Test
    func testPositiveLimitFallbackSupportsCreditsButZeroOrNonfiniteDoesNot() {
        #expect(quota(subscriptions([limit(remaining: nil, used: 25, cap: 200, unit: "credits")])).fraction! == 0.125)
        for cap in [0.0, -.infinity, .infinity, .nan] {
            #expect(quota(subscriptions([limit(remaining: nil, cap: cap)])).fraction == nil)
        }
        #expect(quota(subscriptions([limit(remaining: .nan, used: .infinity)])).fraction == nil)
    }
    @Test
    func testExpiredAndResetWindowsIgnoreStillPresentUsedValue() {
        for window in [limit(remaining: nil, used: 38, status: "expired"), limit(reset: at), limit(reset: at - 1)] {
            let value = quota(subscriptions([window])); #expect(value.fraction == nil); #expect(value.freshness == .unknown)
        }
    }
    @Test
    func testWindowStatusTakesPrecedenceOverOtherAvailableWindows() {
        let value = quota(subscriptions([limit(status: "stale"), limit(key: "primary", minutes: 300)]))
        #expect(value.freshness == .stale)
    }
    @Test
    func testOldAndFutureObservationBecomeStaleWithoutLosingRatio() {
        for window in [limit(age: 901), limit(age: -61), limit(status: "stale")] {
            let value = quota(subscriptions([window])); #expect(value.freshness == .stale); #expect(value.fraction != nil)
        }
        #expect(quota(subscriptions([limit(age: 900)])).freshness == .valid)
        #expect(quota(subscriptions([limit(age: -60)])).freshness == .valid)
    }
    @Test
    func testPlatformErrorAndFetchFailurePreserveOldQuotaAsStale() {
        for value in [quota(subscriptions(status: "error")), quota(failed: true)] {
            #expect(value.freshness == .stale); #expect(abs((value.fraction!) - (0.38)) <= 1e-12)
            #expect(value.description.contains("查询失败"))
        }
    }
    @Test
    func testUnknownOrUnauthorizedQuotaCannotPretendToBeUnused() {
        #expect(quota(subscriptions([], status: "unavailable")).freshness == .unknown)
        #expect(quota(subscriptions(status: "auth_required")).fraction == nil)
        #expect(QuotaPresentation.evaluate(snapshot: subscriptions(), selection: nil, failed: false, now: at).fraction == nil)
        #expect(QuotaPresentation.evaluate(snapshot: nil, selection: identity, failed: true, now: at).fraction == nil)
    }
    @Test
    func testInitialSelectionUsesActualWeeklyDurationAndKeepsExactBucket() {
        var choice = QuotaSelection()
        let modelWeekly = limit(key: "primary", bucket: "model-special", minutes: 10080)
        let wrongPrimary = limit(key: "secondary", minutes: 300)
        #expect(choice.resolve(snapshot: subscriptions([wrongPrimary, modelWeekly]), now: at) == QuotaIdentity(tool: "codex", bucket: "model-special", key: "primary"))
    }
    @Test
    func testSelectedSourceDoesNotSwitchWhenMissingOrExpired() {
        var choice = QuotaSelection(identity: identity)
        #expect(choice.resolve(snapshot: subscriptions([limit(key: "other")]), now: at) == identity)
        #expect(quota(subscriptions([limit(key: "other")])).fraction == nil)
        #expect(choice.resolve(snapshot: subscriptions([limit(status: "expired")]), now: at) == identity)
    }
    @Test
    func testInitialSelectionDoesNotBorrowOtherPlatformOrOldQuota() {
        var choice = QuotaSelection()
        #expect(choice.resolve(snapshot: subscriptions(tool: "claude"), now: at) == nil)
        #expect(choice.resolve(snapshot: subscriptions([limit(status: "stale")]), now: at) == nil)
        #expect(choice.resolve(snapshot: subscriptions(), now: at) == identity)
        choice.select(nil)
        #expect(choice.resolve(snapshot: subscriptions(), now: at) == nil)
    }
    @Test
    func testQuotaIdentityIncludesToolBucketAndKey() {
        #expect(Set([identity, QuotaIdentity(tool: "claude", bucket: "codex", key: "secondary"),
                            QuotaIdentity(tool: "codex", bucket: "model", key: "secondary"),
                            QuotaIdentity(tool: "codex", bucket: "codex", key: "primary")]).count == 4)
    }
    @Test
    func testSubscriptionsDecodeNullWithoutInventingAQuota() throws {
        let raw = #"{"demo":true,"now":1800000000,"platforms":[{"tool":"codex","name":"Codex","status":"stale","updated_at":1799999990,"limits":[{"key":"secondary","bucket":"codex","label":"每周额度","source":"codex_record","observed_at":1799999990,"unit":"percent","status":"expired","limit":100,"used":38,"remaining":null,"remaining_percent":null,"window_minutes":10080,"resets_at":1800000000}]}]}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(SubscriptionSnapshot.self, from: Data(raw.utf8))
        #expect(value.platforms[0].limits[0].remainingPercent == nil)
        #expect(value.platforms[0].limits[0].used == 38)
        #expect(quota(value).fraction == nil)
    }
    @Test
    func testMissingObservationTimeIsUnknownEvenWithAReportedRatio() {
        let window = SubscriptionLimit(key: "secondary", bucket: "codex", label: "每周额度", source: "codex_record",
                                       observedAt: nil, remainingPercent: 62)
        #expect(quota(subscriptions([window])).freshness == .unknown)
    }
    @Test
    func testSubscriptionsFailureDoesNotInvalidateIndependentState() {
        #expect(quota(failed: true).freshness == .stale)
        let value = CapsuleStatus.evaluate(snapshot: snapshot(main: counts(active: 2, waiting: 1)), failed: false, age: 0)
        #expect(value.active == 2); #expect(value.fresh)
    }
    @Test
    func testCapsulePerimeterUsesFullCurvedOutline() {
        #expect(abs((CapsuleGeometry.perimeter(width: 220, height: 38)) - (364 + .pi * 38)) <= 1e-12)
        #expect(abs((CapsuleGeometry.perimeter(width: 38, height: 38)) - (.pi * 38)) <= 1e-12)
        #expect(CapsuleGeometry.perimeter(width: 0, height: 38) == 0)
        #expect(CapsuleGeometry.perimeter(width: .nan, height: 38) == 0)
    }
    @Test
    func testHighlightNeverLeavesUsedSegmentAcrossPhasesAndBoundaryFractions() {
        for fraction in [0.01, 0.38, 1.0] {
            for step in -100...200 {
                let range = CapsuleGeometry.shineRange(fraction: fraction, phase: Double(step) / 100)!
                #expect(range.lowerBound >= 0)
                #expect(range.upperBound <= fraction)
                #expect(range.upperBound > range.lowerBound)
            }
        }
    }
    @Test
    func testZeroAndInvalidQuotaNeverCreatesAHighlight() {
        for fraction in [0.0, -0.1, .nan, .infinity] { #expect(CapsuleGeometry.shineRange(fraction: fraction, phase: 0.5) == nil) }
        #expect(CapsuleGeometry.shineRange(fraction: 0.38, phase: .nan) == nil)
    }
}
