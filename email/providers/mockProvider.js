/**
 * Mock email provider.
 *
 * This is the only file that knows the email data is fake. It exposes one
 * async function, fetchInbox(), returning EmailRecord[] (see ../types.js).
 * A future real provider (e.g. providers/gmailProvider.js) would implement
 * the same fetchInbox() signature — state.js and the UI would not change.
 */

import { FOLDER } from '../types.js';

const NETWORK_DELAY_MS = 650;

// Mutated in place by state.js actions (mark as DLYTE, archive, junk) so
// that changes persist across refresh() calls during the session.
const seed = [
  {
    id: 'e1',
    senderName: 'Court Registry — Palais de Justice de Montréal',
    senderEmail: 'registry@judex-mtl.example.ca',
    recipients: ['you@example.com'],
    subject: 'Case management deadline reminder',
    preview: 'This is a reminder that outstanding documents are due into the record before the next hearing.',
    body: 'This is a reminder that outstanding documents (tax returns, notices of assessment, and Form III) are due into the record before the next hearing. Please file directly with the Court and copy the assigned judicial assistant.',
    receivedAt: '2026-07-24T16:10:00',
    unread: true,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e2',
    senderName: 'Me. Sophie Bernard, Family Counsel',
    senderEmail: 's.bernard@bernardavocats.example',
    recipients: ['you@example.com'],
    subject: 'Draft settlement terms for review',
    preview: 'Please find attached our proposed terms regarding parenting time and video call scheduling.',
    body: 'Please find attached our proposed terms regarding parenting time, video call scheduling, and reporting cadence. Let us know your comments by end of week so we can finalize before the hearing.',
    receivedAt: '2026-07-23T11:05:00',
    unread: true,
    attachments: [{ name: 'Draft-Settlement-Terms.pdf' }],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e3',
    senderName: 'L. Moreau',
    senderEmail: 'l.moreau.personal@example.com',
    recipients: ['you@example.com'],
    subject: "Re: Sophia's school documents",
    preview: 'Attached is the enrollment confirmation you asked for.',
    body: "Attached is the enrollment confirmation you asked for. Let me know if the school needs anything else from your side before the term starts.",
    receivedAt: '2026-07-22T09:40:00',
    unread: false,
    attachments: [{ name: 'Enrollment-Confirmation.pdf' }],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e4',
    senderName: 'Scotiabank Alerts',
    senderEmail: 'alerts@scotiabank.example.com',
    recipients: ['you@example.com'],
    subject: 'Your July e-statement is ready',
    preview: 'Your monthly account statement is now available to view online.',
    body: 'Your monthly account statement is now available to view online. Log in to your account to download a copy for your records.',
    receivedAt: '2026-07-21T08:00:00',
    unread: false,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e5',
    senderName: 'Revenu Québec',
    senderEmail: 'noreply@revenuquebec.example.ca',
    recipients: ['you@example.com'],
    subject: 'Notice of Assessment available',
    preview: 'Your 2024 notice of assessment is now available in My Account.',
    body: 'Your 2024 notice of assessment is now available in My Account. A copy is attached for your records.',
    receivedAt: '2026-07-20T14:22:00',
    unread: true,
    attachments: [{ name: 'NOA-2024.pdf' }],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e6',
    senderName: 'Family Mediation Services',
    senderEmail: 'intake@mediationqc.example.org',
    recipients: ['you@example.com'],
    subject: 'Scheduling your mediation intake call',
    preview: "We'd like to schedule a 30-minute intake call at your convenience.",
    body: "We'd like to schedule a 30-minute intake call at your convenience to go over the mediation process and next steps. Reply with a few times that work for you.",
    receivedAt: '2026-07-19T13:15:00',
    unread: false,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e7',
    senderName: 'Parenting Weekly',
    senderEmail: 'newsletter@parentingweekly.example.com',
    recipients: ['you@example.com'],
    subject: '5 tips for co-parenting across time zones',
    preview: 'This week: managing video calls, school schedules, and more.',
    body: 'This week: managing video calls across time zones, keeping school schedules in sync, and staying connected during long-distance stretches. Read more on our blog.',
    receivedAt: '2026-07-19T07:00:00',
    unread: true,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e8',
    senderName: 'Amazon.ca',
    senderEmail: 'shipment-tracking@amazon.example.ca',
    recipients: ['you@example.com'],
    subject: 'Your package has shipped',
    preview: 'Your order of 1 item is on its way, arriving Jul 27.',
    body: 'Your order of 1 item is on its way, arriving Jul 27. Track your package for real-time updates.',
    receivedAt: '2026-07-18T19:32:00',
    unread: false,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e9',
    senderName: 'Air Transat',
    senderEmail: 'noreply@airtransat.example.com',
    recipients: ['you@example.com'],
    subject: 'Your itinerary confirmation — YUL to CDG',
    preview: "Thanks for booking with us. Here's your itinerary summary.",
    body: "Thanks for booking with us. Here's your itinerary summary, including check-in and baggage details for your upcoming flight.",
    receivedAt: '2026-07-15T10:00:00',
    unread: false,
    attachments: [{ name: 'Itinerary-AT945.pdf' }],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e10',
    senderName: 'Planet Fitness',
    senderEmail: 'promotions@planetfit.example.com',
    recipients: ['you@example.com'],
    subject: 'Summer sale: 50% off enrollment',
    preview: 'Join now and save on your first three months.',
    body: 'Join now and save on your first three months. Offer ends soon — visit any club location to sign up.',
    receivedAt: '2026-07-14T12:00:00',
    unread: true,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e11',
    senderName: 'Marc Tremblay',
    senderEmail: 'marc.tremblay@example.com',
    recipients: ['you@example.com'],
    subject: 'Long time no talk!',
    preview: "Hey, was thinking about you — how's everything going these days?",
    body: "Hey, was thinking about you — how's everything going these days? We should catch up sometime, it's been way too long.",
    receivedAt: '2026-07-12T20:45:00',
    unread: false,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
  {
    id: 'e12',
    senderName: 'Special Clerk, Room 2.17',
    senderEmail: 'clerk217@judex-mtl.example.ca',
    recipients: ['you@example.com'],
    subject: 'Hearing room confirmation',
    preview: 'This confirms your matter will be heard as scheduled.',
    body: 'This confirms your matter will be heard as scheduled. Please arrive at least 15 minutes early to check in with the courtroom clerk.',
    receivedAt: '2026-07-17T15:50:00',
    unread: false,
    attachments: [],
    folder: FOLDER.INBOX,
    labels: [],
  },
];

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function fetchInbox() {
  await delay(NETWORK_DELAY_MS);

  // Test hook only: ?mockError=1 exercises the error state without
  // relying on flaky/random failures.
  if (new URLSearchParams(window.location.search).has('mockError')) {
    throw new Error('Simulated inbox load failure');
  }

  return seed;
}
