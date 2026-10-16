import Foundation
import Testing
@testable import CapsuleCore

@Suite("Cursor dashboard quota snapshots")
struct CursorQuotaTests {
    let at: TimeInterval = 1_800_000_000
    let identity = QuotaIdentity(tool: "cursor", bucket: "cursor_models", key: "monthly")

    func window(status: String = "available", observedAt: TimeInterval? = nil,
                capturedAt: TimeInterval? = nil, source: String = "cursor_dashboard_ui",
                remaining: Double? = 63, key: String = "monthly", validUntil: TimeInterval? = nil) -> SubscriptionLimit {
        SubscriptionLimit(key: key, bucket: "cursor_models", label: "Cursor Models · 每月",
                          source: source, observedAt: observedAt, status: status,
                          limit: 100, remainingPercent: remaining,
                          displayPrecision: "integer_percent", capturedAt: capturedAt,
                          resetLabel: "页面显示 10 月 20 日重置", validUntil: validUntil)
    }

    func snapshot(_ limit: SubscriptionLimit, status: String = "available",
                  capturedAt: TimeInterval? = nil) -> SubscriptionSnapshot {
        SubscriptionSnapshot(demo: true, now: at, platforms: [
            SubscriptionPlatform(tool: "cursor", name: "Cursor", status: status,
                                 capturedAt: capturedAt, limits: [limit])
        ])
    }

    func evaluate(_ value: SubscriptionSnapshot, failed: Bool = false,
                  now: TimeInterval? = nil) -> QuotaPresentation {
        QuotaPresentation.evaluate(snapshot: value, selection: identity, failed: failed, now: now ?? at)
    }

    @Test
    func decodePageCaptureWithoutInventingOfficialUpdateOrExactReset() throws {
        let raw = #"{"demo":true,"now":1800000000,"platforms":[{"tool":"cursor","name":"Cursor","status":"available","updated_at":null,"captured_at":1800000000,"limits":[{"key":"monthly","bucket":"cursor_models","label":"Cursor Models · 每月","source":"cursor_dashboard_ui","observed_at":1800000000,"captured_at":1800000000,"display_precision":"integer_percent","unit":"percent","status":"available","limit":100,"used":37,"remaining_percent":63,"resets_at":null,"reset_label":"页面显示 10 月 20 日重置","valid_until":1800010000}]}]}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(SubscriptionSnapshot.self, from: Data(raw.utf8))
        let platform = value.platforms[0], limit = platform.limits[0]
        #expect(platform.updatedAt == nil)
        #expect(platform.capturedAt == at)
        #expect(limit.capturedAt == at)
        #expect(limit.displayPrecision == "integer_percent")
        #expect(limit.resetLabel == "页面显示 10 月 20 日重置")
        #expect(limit.resetsAt == nil)
        #expect(limit.validUntil == at + 10_000)
        let result = evaluate(value)
        #expect(result.freshness == .valid)
        #expect(abs(result.fraction! - 0.37) < 1e-12)
        #expect(result.description.contains("页面快照 · 约已用 37%"))
        #expect(result.description.contains("页面显示 10 月 20 日重置"))
        #expect(!result.description.contains("37.0%"))
    }

    @Test
    func oldApiResponsesStillDecodeWithoutNewFields() throws {
        let raw = #"{"demo":true,"now":1800000000,"platforms":[{"tool":"codex","name":"Codex","status":"available","limits":[{"key":"secondary","bucket":"codex","label":"每周额度","source":"codex_record","observed_at":1800000000,"unit":"percent","status":"available","remaining_percent":63}]}]}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let value = try decoder.decode(SubscriptionSnapshot.self, from: Data(raw.utf8))
        #expect(value.platforms[0].capturedAt == nil)
        let limit = value.platforms[0].limits[0]
        #expect(limit.displayPrecision == nil)
        #expect(limit.capturedAt == nil)
        #expect(limit.resetLabel == nil)
    }

    @Test
    func explicitPageCaptureCanSupplyObservationFreshnessOnly() {
        for value in [snapshot(window(capturedAt: at)), snapshot(window(), capturedAt: at),
                      snapshot(window(observedAt: at))] {
            #expect(evaluate(value).freshness == .valid)
            #expect(value.platforms[0].updatedAt == nil)
        }
        #expect(evaluate(snapshot(window())).freshness == .unknown)
        #expect(evaluate(snapshot(window(capturedAt: .nan))).freshness == .unknown)
        #expect(evaluate(snapshot(window(capturedAt: 0))).freshness == .unknown)
    }

    @Test
    func captureCannotRepairUnknownOrExistingNonPageSources() {
        for source in ["codex_record", "future_source"] {
            let result = evaluate(snapshot(window(capturedAt: at, source: source), capturedAt: at))
            #expect(result.freshness == .unknown)
            #expect(result.fraction == nil)
        }
        let result = evaluate(snapshot(window(observedAt: at, capturedAt: at, source: "future_source")))
        #expect(result.freshness == .valid)
        #expect(!result.description.contains("页面快照"))
    }

    @Test
    func stalePageValuesKeepBothApproximationAndCaptureLabel() {
        let value = snapshot(window(capturedAt: at))
        #expect(evaluate(value, now: at + 900).freshness == .valid)
        for result in [evaluate(value, now: at + 901), evaluate(value, now: at - 61),
                       evaluate(snapshot(window(status: "stale", capturedAt: at))),
                       evaluate(snapshot(window(capturedAt: at), status: "stale")),
                       evaluate(value, failed: true)] {
            #expect(result.freshness == .stale)
            #expect(result.description.contains("页面快照 · 旧快照 · 约已用 37%"))
        }
        let failed = evaluate(value, failed: true)
        #expect(failed.description.contains("读取失败，保留页面值"))
        #expect(!failed.description.contains("查询失败"))
    }

    @Test
    func expiredPageDoesNotLookUnusedOrPromiseAutomaticRefresh() {
        let result = evaluate(snapshot(window(status: "expired", capturedAt: at)))
        #expect(result.freshness == .unknown)
        #expect(result.fraction == nil)
        #expect(result.description == "进入可能重置日期，等待新快照")
        #expect(evaluate(snapshot(window(capturedAt: at, remaining: nil))).fraction == nil)
        #expect(evaluate(snapshot(window(capturedAt: at, remaining: 100))).fraction == 0)
    }

    @Test
    func dateOnlyValidityCutoffExpiresWithoutInventingResetTimestamp() {
        let value = snapshot(window(capturedAt: at, validUntil: at + 10))
        #expect(value.platforms[0].limits[0].resetsAt == nil)
        #expect(evaluate(value, now: at + 9).freshness == .valid)
        let result = evaluate(value, now: at + 10)
        #expect(result.freshness == .unknown)
        #expect(result.fraction == nil)
        #expect(result.description == "进入可能重置日期，等待新快照")
        #expect(evaluate(value, failed: true, now: at + 11).fraction == nil)
        #expect(evaluate(snapshot(window(capturedAt: at, validUntil: .nan))).freshness == .unknown)
        // An explicit observed time remains authoritative over capture metadata.
        #expect(evaluate(snapshot(window(observedAt: at - 901, capturedAt: at))).freshness == .stale)
    }

    @Test
    func cursorPageNeverReplacesAutomaticCodexWeeklySelection() {
        var selection = QuotaSelection()
        let cursor = snapshot(window(capturedAt: at))
        #expect(selection.resolve(snapshot: cursor, now: at) == nil)
        let codex = SubscriptionLimit(key: "secondary", bucket: "codex", label: "每周额度",
                                      source: "codex_record", observedAt: at,
                                      remainingPercent: 80, windowMinutes: 10080)
        let combined = SubscriptionSnapshot(demo: true, now: at, platforms: cursor.platforms + [
            SubscriptionPlatform(tool: "codex", name: "Codex", status: "available", limits: [codex])
        ])
        #expect(selection.resolve(snapshot: combined, now: at) == QuotaIdentity(tool: "codex", bucket: "codex", key: "secondary"))
        selection.select(identity)
        #expect(selection.resolve(snapshot: combined, now: at) == identity)
        #expect(selection.resolve(snapshot: snapshot(window(capturedAt: at, key: "weekly")), now: at) == identity)
        #expect(evaluate(snapshot(window(capturedAt: at, key: "weekly"))).fraction == nil)
    }
}
