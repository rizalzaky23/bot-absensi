"""
Smoke test end-to-end untuk main.py memakai mock Moodle + Telegram palsu.

Tidak menyentuh jaringan: seluruh request di-intercept oleh Playwright route.

Jalankan:
    .venv/bin/python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from main import AttendanceResult, SpadaBot, TelegramNotifier, build_summary  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture HTML
# ---------------------------------------------------------------------------

LOGIN_PAGE = """
<html><body>
<form action="https://spada.upnyk.ac.id/login/index.php" method="post">
  <input type="text" name="username" id="username" autocomplete="username">
  <input type="password" name="password" id="password">
  <button id="loginbtn" type="submit">Log in</button>
</form>
</body></html>
"""

LOGIN_FAILED_PAGE = """
<html><body>
<div class="alert alert-danger" id="loginerrormessage">Invalid login, please try again</div>
<a href="https://spada.upnyk.ac.id/login/index.php">Log in</a>
</body></html>
"""

DASHBOARD_PAGE = """
<html><body>
<div class="usermenu">Dashboard</div>
<a href="https://spada.upnyk.ac.id/login/logout.php">Log out</a>
</body></html>
"""

ATTENDANCE_OPEN_PAGE = """
<html><body>
<h1>Keamananpriced Software Engineering</h1>
<table id="attendancetable" class="generaltable">
<thead><tr><th>Date</th><th>Time</th><th>Status</th><th>Comment</th><th>Actions</th></tr></thead>
<tbody>
<tr><td>01 Sep 2026</td><td>08:00</td><td>Closed</td><td></td><td></td></tr>
<tr><td>29 Sep 2026</td><td>10:00</td><td>Not yet open</td><td></td><td></td></tr>
<tr><td>29 Sep 2026</td><td>16:00</td><td>Needs your attention</td><td></td>
  <td><a href="https://spada.upnyk.ac.id/mod/attendance/attendance_submit.php?id=12345&amp;sessionid=9">Submit attendance</a></td></tr>
</tbody></table>
</body></html>
"""

# Tanpa id pada tabel: memaksa fallback selector baris sesi.
ATTENDANCE_OPEN_NOID_PAGE = ATTENDANCE_OPEN_PAGE.replace(
    'id="attendancetable" ', ""
)

# Radio tanpa value="0": memaksa fallback pemilihan radio.
ATTENDANCE_SUBMIT_FORM_NO_VALUE = """
<html><body>
<h1>Keamananpriced Software Engineering</h1>
<form method="post" action="https://spada.upnyk.ac.id/mod/attendance/attendance_submit.php">
  <label><input type="radio" name="presence"> Hadir</label>
  <label><input type="radio" name="presence"> Tidak Hadir</label>
  <input type="submit" name="submitbutton" value="Simpan">
</form>
</body></html>
"""

ATTENDANCE_CLOSED_PAGE = """
<html><body>
<h1>Metodologi Penelitian</h1>
<table class="generaltable">
<tr><th>Date</th><th>Time</th><th>Status</th><th>Actions</th></tr>
<tr><td>29 Sep 2026</td><td>09:00</td><td>Closed</td><td></td></tr>
</table>
</body></html>
"""

ATTENDANCE_DONE_PAGE = """
<html><body>
<h1>Keamananpriced Software Engineering</h1>
<table class="generaltable">
<tr><td>29 Sep 2026</td><td>16:00</td><td class="attendancestatus">Present</td><td></td></tr>
</table>
</body></html>
"""

ATTENDANCE_SUBMIT_FORM = """
<html><body>
<h1>Keamananpriced Software Engineering</h1>
<form method="post" action="https://spada.upnyk.ac.id/mod/attendance/attendance_submit.php">
  <input type="hidden" name="id" value="12345">
  <input type="hidden" name="sessionid" value="9">
  <label><input type="radio" name="presence" value="0" checked> Present</label>
  <label><input type="radio" name="presence" value="1"> Absent</label>
  <label><input type="radio" name="presence" value="2"> Excused</label>
  <input type="submit" name="submitbutton" value="Save changes">
</form>
</body></html>
"""

ATTENDANCE_SUBMIT_DONE = """
<html><body>
<div class="alert alert-success">Attendance for 29 Sep 2026 has been recorded: Present</div>
<h1>Keamananpriced Software Engineering</h1>
<table class="generaltable">
<tr><td>29 Sep 2026</td><td>16:00</td><td class="attendancestatus">Present</td><td></td></tr>
</table>
</body></html>
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_config(tmp: Path, targets: list[str]) -> main.Config:
    """Config dummy yang tidak membaca .env sungguhan."""
    return main.Config(
        username="student",
        password="secret",
        telegram_token="t",
        telegram_chat_id="c",
        headless=True,
        attendance_targets=targets,
        timeout_ms=4_000,
        artifact_dir=tmp,
    )


async def run_scenario(handler, targets: list[str], tmp: Path) -> AttendanceResult:
    """Jalankan login + submit_attendance dengan mock route tertentu."""
    config = make_config(tmp, targets)
    bot = SpadaBot(config)
    try:
        await bot.start()
        await bot.context.route("**/*", handler)
        await bot.login()
        return await bot.submit_attendance(targets[0])
    finally:
        await bot.close()


async def main_test() -> int:
    failures: list[str] = []
    tmp = Path(".artifacts-test")
    tmp.mkdir(exist_ok=True)

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}{(' -> ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(label)

    def build_mock(view_page: str, form_page: str = ATTENDANCE_SUBMIT_FORM):
        """Buat route handler dengan halaman view & form submit yang bisa diatur."""

        async def handler(route):
            url, method = route.request.url, route.request.method
            if url.endswith("/login/index.php"):
                return await fulfill(route, DASHBOARD_PAGE if method == "POST" else LOGIN_PAGE)
            if "attendance_submit" in url and method == "POST":
                return await fulfill(route, ATTENDANCE_SUBMIT_DONE)
            if "attendance_submit" in url:
                return await fulfill(route, form_page)
            if "/mod/attendance/view.php" in url:
                return await fulfill(route, view_page)
            return await fulfill(route, DASHBOARD_PAGE)

        return handler

    print("\n1. Sesi aktif -> submit 'Hadir'")
    result = await run_scenario(build_mock(ATTENDANCE_OPEN_PAGE), ["12345"], tmp)
    check("status success", result.status == "success", f"status={result.status} reason={result.reason}")
    check("course name terdeteksi", "Software Engineering" in result.course, result.course)

    print("\n1b. Fallback selector tabel & radio tanpa value")
    result = await run_scenario(
        build_mock(ATTENDANCE_OPEN_NOID_PAGE, ATTENDANCE_SUBMIT_FORM_NO_VALUE), ["12345"], tmp
    )
    check("tetap success tanpa id tabel", result.status == "success", f"status={result.status} reason={result.reason}")

    print("\n2. Sudah absen hari ini")
    result = await run_scenario(build_mock(ATTENDANCE_DONE_PAGE), ["12345"], tmp)
    check("status already", result.status == "already", f"status={result.status}")

    print("\n3. Sesi tertutup")
    result = await run_scenario(build_mock(ATTENDANCE_CLOSED_PAGE), ["12345"], tmp)
    check("status not_open", result.status == "not_open", f"status={result.status}")
    check("alasan jelas", "ditutup" in result.reason or "No active" in result.reason, result.reason)
    check("screenshot tersimpan", any(tmp.glob("no-session-*.png")))

    print("\n4. Login gagal")

    async def mock_bad_login(route):
        url, method = route.request.url, route.request.method
        if url.endswith("/login/index.php"):
            return await fulfill(route, LOGIN_FAILED_PAGE if method == "POST" else LOGIN_PAGE)
        return await fulfill(route, DASHBOARD_PAGE)

    config = make_config(tmp, ["12345"])
    bot = SpadaBot(config)
    try:
        await bot.start()
        await bot.context.route("**/*", mock_bad_login)
        try:
            await bot.login()
            check("login ditolak", False, "tidak raise SpadaError")
        except main.SpadaError as exc:
            check("login ditolak", "Login gagal" in str(exc), str(exc))
    finally:
        await bot.close()

    print("\n5. Format pesan Telegram")
    success_msg = TelegramNotifier.format_success("Basis Data", main.now_local(), "ok")
    check("pesan sukses memuat judul", "Presensi SPADA Berhasil" in success_msg)
    failure_msg = TelegramNotifier.format_failure("Basis Data", main.now_local(), "No active session found")
    check("pesan gagal memuat penyebab", "No active session found" in failure_msg)
    check("pesan gagal memuat waktu", "Waktu Cek" in failure_msg)

    print("\n6. Ringkasan multi-mata kuliah")
    summary = build_summary(
        [
            AttendanceResult("Basis Data", "success", "Dikonfirmasi"),
            AttendanceResult("Jaringan", "already", "Sudah absen"),
            AttendanceResult("Kalkulus", "not_open", "Sesi ditutup"),
            AttendanceResult("AI", "failed", "Selector timeout"),
        ]
    )
    check("ringkasan lengkap", "Ringkasan Presensi SPADA" in summary and "Kalkulus" in summary)

    print("\n7. Kirim ke server Telegram palsu (HTTP lokal)")
    received: list[dict] = []

    async def handler(reader, writer):
        await handle_telegram(reader, writer, received)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    main.TELEGRAM_API_BASE = f"http://127.0.0.1:{port}"
    try:
        notifier = TelegramNotifier("TOKEN", "-100123")
        check("sukses terkirim", await notifier.send(success_msg))
        payload = received[0] if received else {}
        check("payload benar", payload.get("chat_id") == "-100123" and payload.get("text") == success_msg)
        check("parse_mode Markdown", payload.get("parse_mode") == "Markdown")
        check("path API benar", payload.get("path") == "/botTOKEN/sendMessage", str(payload.get("path")))
    finally:
        main.TELEGRAM_API_BASE = "https://api.telegram.org"
        server.close()
        await server.wait_closed()

    print("\n8. Utilitas")
    check("resolve cmid", main.resolve_target("999") == "https://spada.upnyk.ac.id/mod/attendance/view.php?id=999")
    check(
        "resolve URL passthrough",
        main.resolve_target("https://x.test/mod/attendance/view.php?id=1")
        == "https://x.test/mod/attendance/view.php?id=1",
    )
    check("session_reason kosong", "No active session" in main.session_reason(""))

    print()
    if failures:
        print(f"{len(failures)} test GAGAL: {failures}")
        return 1
    print("Semua test lulus.")
    return 0


async def fulfill(route, body: str):
    """Bungkus route.fulfill agar handler tetap ringkas."""
    return await route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=body
    )


async def handle_telegram(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, sink: list
) -> None:
    """Server HTTP satu-shot yang mencatat payload Telegram lalu membalas ok."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        body = json.loads(await reader.readexactly(length) if length else b"{}")
        body["path"] = lines[0].split(" ")[1]
        sink.append(body)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"ok": true}')
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
