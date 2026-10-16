import Foundation
#if canImport(CapsuleCore)
import CapsuleCore
#endif

/// Exercises the native models and transport against an explicitly supplied
/// Python --demo service. Nothing beyond the first snapshot is requested until
/// its demo marker is verified. Actions affect this temporary demo only.
@main
struct DemoCheck {
    private struct Requested: Decodable { let requested: Bool }
    private struct Dismissed: Decodable { let dismissed: Bool }
    private enum Failure: Error { case check(String) }

    private static func require(_ value: Bool, _ label: String) throws {
        guard value else { throw Failure.check(label) }
    }

    static func main() async {
        guard CommandLine.arguments.count == 2,
              let origin = URL(string: CommandLine.arguments[1]), origin.port != 8766 else {
            print("FAIL: explicit_demo_origin=false")
            exit(2)
        }
        var client: APIClient?
        do {
            let connection = try APIClient(baseURL: origin)
            client = connection
            let path = "/api/snapshot?tool=all&project=all&role=main"
            let snapshot = try await connection.get(path, as: Snapshot.self)
            // Fail closed before subscriptions, children, or any action.
            try require(snapshot.demo, "demo_guard")
            print("PASS: demo_guard=true")

            try require(snapshot.sessions.allSatisfy { $0.role == "main" }, "main_role")
            try require(snapshot.childrenCounts != nil, "complete_aggregate")
            try require(snapshot.attentionCounts?.pending == 2, "explicit_attention_aggregate")
            try require(snapshot.inboxCounts?.pending == 3 && snapshot.inboxCounts?.actionRequired == 2 &&
                        snapshot.inboxCounts?.unreadCompleted == 1, "explicit_inbox_aggregate")
            let status = CapsuleStatus.evaluate(snapshot: snapshot, failed: false, age: 0)
            try require(status.fresh && status.active == 1 && status.pending == 3, "complete_counts")
            try require(snapshot.recentCompleted?.contains { $0.id == "codex:demo-complete" && $0.lastCompletedAt != nil && $0.unreadCompleted == true } == true,
                        "reliable_recent_completion")
            let titled = snapshot.sessions.first { $0.id == "codex:demo-complete" }
            try require(titled?.sessionTitle == "演示会话 · 文档校对" && titled?.displayName == titled?.projectDisplayName &&
                        titled?.projectDisplayName == "演示长项目名称用于检查悬停展开与十字符限制" &&
                        titled?.projectName == "g-p-demo-title-project" &&
                        (titled?.displayName.count ?? 0) > 10, "native_project_name_precedence")
            print("PASS: native_project_name_precedence=true session_title_preserved=true")
            let groups = CapsuleDetailGroups.evaluate(snapshot: snapshot, now: snapshot.now)
            try require(groups.pending.count == 2 && groups.active.isEmpty && groups.completed.contains { $0.id == "codex:demo-complete" },
                        "three_detail_groups")
            try require(Set((groups.pending + groups.active + groups.completed).map(\.id)).count ==
                        groups.pending.count + groups.active.count + groups.completed.count, "unique_root_rows")
            print("PASS: main_role=true complete_counts=true explicit_attention_aggregate=true explicit_inbox_aggregate=true reliable_recent_completion=true three_detail_groups=true unique_root_rows=true")

            let subscriptions = try await connection.get("/api/subscriptions", as: SubscriptionSnapshot.self)
            try require(subscriptions.demo, "subscription_demo_guard")
            let tools = Set(subscriptions.platforms.map(\.tool))
            try require(tools.isSuperset(of: ["codex", "claude", "workbuddy"]), "subscription_platform_decode")
            try require(subscriptions.platforms.contains { platform in
                platform.tool == "codex" && platform.limits.contains {
                    $0.windowMinutes == 10080 && $0.remainingPercent?.isFinite == true && !$0.bucket.isEmpty && !$0.key.isEmpty
                }
            }, "subscription_window_decode")
            print("PASS: subscription_decode=true")

            guard let parent = snapshot.sessions.first(where: { $0.childrenSummary?.total == 6 }) else {
                throw Failure.check("explicit_parent")
            }
            var childPath = URLComponents()
            childPath.path = "/api/children"
            childPath.queryItems = [URLQueryItem(name: "id", value: parent.id)]
            let children = try await connection.get(childPath.string!, as: ChildrenResponse.self)
            try require(children.demo && children.parentId == parent.id && !children.truncated,
                        "explicit_children_guard")
            try require(children.children.count == 6 && children.counts.total == 6 &&
                        children.children.allSatisfy { $0.role == "child" }, "explicit_children")
            try require(children.children.contains { $0.depth == 2 }, "depth_two")
            try require(snapshot.sessions.allSatisfy { $0.jump?.available == false } &&
                        children.children.allSatisfy { $0.jump?.available == false }, "jump_disabled")
            print("PASS: explicit_children=true depth_two=true jump_disabled=true")

            let requested = try await connection.post("/api/rescan", body: [:], as: Requested.self)
            try require(requested.requested, "rescan_csrf")
            print("PASS: rescan_csrf=true empty_body=true")

            let refreshed = try await connection.get(path, as: Snapshot.self)
            try require(refreshed.demo && refreshed.childrenCounts != nil && refreshed.attentionCounts != nil, "post_rescan_demo_guard")
            let refreshedStatus = CapsuleStatus.evaluate(snapshot: refreshed, failed: false, age: 0)
            try require(refreshedStatus.fresh && refreshedStatus.active == 1 && refreshedStatus.pending == 3,
                        "post_rescan_readable")
            print("PASS: post_rescan_readable=true")

            guard let pending = refreshed.sessions.first(where: { $0.id == parent.id }),
                  let version = pending.attentionDismissVersion else { throw Failure.check("dismiss_version") }
            let dismissed = try await connection.post("/api/attention/dismiss",
                body: ["id": pending.id, "version": version], as: Dismissed.self)
            try require(dismissed.dismissed, "demo_dismiss_csrf")
            let reconciled = try await connection.get(path, as: Snapshot.self)
            try require(reconciled.demo && reconciled.attentionCounts?.pending == 1 &&
                        reconciled.inboxCounts?.pending == 2 && reconciled.counts.active == 1,
                        "dismiss_aggregate")
            try require(reconciled.sessions.first(where: { $0.id == parent.id })?.requiresHumanAttention == false,
                        "dismiss_visible_descendants")
            try require(reconciled.inboxCounts?.unreadCompleted == 1, "dismiss_preserves_unread_completion")
            do {
                _ = try await connection.post("/api/attention/dismiss", body: ["id": pending.id, "version": version], as: Dismissed.self)
                throw Failure.check("old_dismiss_version_rejected")
            } catch APIClientError.httpStatus(409) { }
            print("PASS: demo_dismiss_csrf=true dismiss_aggregate=true dismiss_visible_descendants=true dismiss_preserves_unread_completion=true old_dismiss_version_rejected=true")
            await connection.close()
        } catch {
            if let client { await client.close() }
            if case Failure.check(let label) = error { print("FAIL: " + label + "=false") }
            else { print("FAIL: native_demo_check=false") }
            exit(1)
        }
    }
}
