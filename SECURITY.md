# Local processing and disclosure

This application is intended for a single user's local machine. Its HTTP service binds only to loopback. Do not expose it through a public proxy or tunnel.

Documents, OCR caches and detection reports can contain sensitive text. They remain in the local runtime directory. To resume after a reload, the browser tab also keeps your search words and review choices in session storage, which is cleared when the tab closes. Closing a document deletes its session files and any zip export that contains it; anything left from an earlier run is deleted when the app starts again. Deletion does not guarantee secure erasure from disks or backups.

The app has no telemetry, remote inference, external fonts, analytics or external browser assets. Initial dependency and OCR model downloads are network operations. Processing uses local libraries and models.

Exports rebuild visible page content from pixels. They strip hidden text, annotations, links, metadata, bookmarks and attachments. Detection can still miss visible identifying content: inspect every exported page.

PDF parsing is not a malware sandbox. The local development HTTP server is not a production multi-user server. A hosted service would require separate authentication, process isolation, quotas and security review.

Please report vulnerabilities privately using the repository's private security reporting feature when it is enabled. Avoid posting real PDFs, OCR reports or secrets in public issues. Use a fictional reproduction.
