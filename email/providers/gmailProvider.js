/**
 * Reads locally synced Gmail records from the server (see
 * gmail/drawer_format.py) — never calls Gmail directly from the browser.
 * Same fetchInbox() shape as providers/mockProvider.js so state.js can
 * swap between them without any other code changing.
 */

export async function fetchInbox() {
  const res = await fetch('/api/gmail/messages');
  if (!res.ok) throw new Error('Could not load synced Gmail messages.');
  const data = await res.json();
  return data.messages || [];
}

// Unreviewed inbox rows only carry lightweight info (see bodyLoaded=false).
// This fetches the real body live from Gmail for display, without saving it.
export async function fetchMessageDetail(id) {
  const res = await fetch(`/api/gmail/messages/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error('Could not load this email.');
  return res.json();
}

export async function startPhotoReviewFromEmail(messageId, attachmentIds = null) {
  const res = await fetch('/api/photo-review/from-email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messageId, attachmentIds }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Could not prepare photographed attachment review.');
  return data;
}
