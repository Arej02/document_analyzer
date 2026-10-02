// Pure helpers for the public share page (design_report.md §9.4). No React, so node:test can import it.

// A token is 43 URL-safe base64 characters. Anything else is not worth a request.
const TOKEN = /^[A-Za-z0-9_-]{43}$/

export const shareTokenFromSearch = (search) => {
  const token = new URLSearchParams(search).get('share')
  return token && TOKEN.test(token) ? token : null
}

// The one message for every failure: the server says nothing more, and neither does the page.
export const UNAVAILABLE_MESSAGE = 'This link is not available. It may have expired or been withdrawn.'

export const snapshotHeading = (generatedAt) => {
  const date = generatedAt ? new Date(generatedAt) : null
  return date && Number.isFinite(date.getTime()) ? `Snapshot as of ${date.toLocaleDateString()}` : 'Snapshot'
}

export const publicPdfFilename = (contentDisposition) => {
  const match = /filename="?([^";]+)"?/i.exec(contentDisposition || '')
  return match && /^[\w.-]+$/.test(match[1]) ? match[1] : 'report.pdf'
}
