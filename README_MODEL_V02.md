# Model A v0.2

This version intentionally remains historical-only.

Main change: it predicts the residual around the rolling-5 attendance baseline,
instead of predicting raw attendance directly. This is intended to handle
Lech's changing attendance level over time more robustly.

It also adds:
- opponent draw ratio,
- opponent-scaled baseline,
- recent attendance trend,
- bias reporting,
- per-season metrics,
- automatic top-10 error report.

Live Roboticket data is not used.
