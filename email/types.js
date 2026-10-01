export const FOLDER = Object.freeze({
  INBOX: 'inbox',
  ARCHIVED: 'archived',
  JUNK: 'junk',
});

export const LABEL_DLYTE = 'dlyte';

/**
 * @typedef {Object} EmailRecord
 * @property {string} id
 * @property {string} senderName
 * @property {string} senderEmail
 * @property {string[]} recipients
 * @property {string} subject
 * @property {string} preview
 * @property {string} body
 * @property {string} receivedAt   ISO timestamp
 * @property {boolean} unread
 * @property {{name: string}[]} attachments
 * @property {string} folder       one of FOLDER
 * @property {string[]} labels     e.g. [LABEL_DLYTE]
 */
