import Foundation

public enum CapsuleGeometry {
    /// Length of a capsule's entire outline, not only its straight top edge.
    public static func perimeter(width: Double, height: Double) -> Double {
        guard width.isFinite, height.isFinite, width > 0, height > 0 else { return 0 }
        let diameter = min(width, height)
        return 2 * (max(width, height) - diameter) + .pi * diameter
    }

    /// Both highlight and tail remain inside the fixed used-quota segment.
    /// A genuinely zero used segment has no substitute minimum highlight.
    public static func shineRange(fraction: Double, phase: Double) -> ClosedRange<Double>? {
        guard fraction.isFinite, phase.isFinite, fraction > 0 else { return nil }
        let used = min(1, fraction)
        let length = min(0.075, used * 0.28)
        let normalized = phase - floor(phase)
        let start = max(0, min(used, normalized * (used - length)))
        return start...min(used, start + length)
    }
}
