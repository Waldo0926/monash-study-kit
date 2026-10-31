Sources: Waldo0926/monash-attendance-reminder commit 977ef23005ae72f8847129f55522841ba6fc6557.
extract-rows.js is extractAttendanceRows from extension/reconciliation-v3.js.
shared.js is extension/shared.js, reused unchanged.
attendance_http.parse_day is a Python port of extract-rows.js for browser-free reads; tests/test_attendance_parity.py checks both give the same rows.
