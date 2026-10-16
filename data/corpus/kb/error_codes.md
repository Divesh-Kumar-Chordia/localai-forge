# Desktop and Sync Error Codes

This reference lists error codes shown by the Acme Cloud desktop sync client (version 6.x).

- E-1001: Authentication expired. The desktop client's session token expired. Sign out and sign back in.
- E-1042: Sync conflict. The same file was edited on two devices before syncing. Acme Cloud keeps both versions; the second one is renamed with "(conflicted copy)" and the device name.
- E-1107: Path too long. The full file path exceeds 260 characters on Windows. Shorten folder names or enable long paths in Windows.
- E-2001: Quota exceeded. The team's pooled storage is full and the 14-day grace period has ended. Free up space or buy more storage.
- E-2015: File too large. Single files are limited to 250 GB on all plans (50 GB on Starter).
- E-3003: Selective sync database corrupted. Quit the client and delete the local .acmesync folder; the client rebuilds it on restart without re-downloading files.
- E-4040: Shared link expired or revoked. Ask the owner to create a new link.
- E-5000: Server error. Retry later and check status.acmecloud.io for incidents.

## Collecting logs
On macOS, logs are in ~/Library/Logs/AcmeCloud. On Windows, logs are in %LOCALAPPDATA%\AcmeCloud\logs. Attach the latest log file when contacting support.
