// The studio's name and the sites it links to. Set at build time; nothing else in web/ hard-codes them.
// The name is a placeholder until the domain is final, so it lives in one place.
export const NAME = import.meta.env.VITE_STUDIO_NAME || 'Impromptune'
export const SITE_URL = import.meta.env.VITE_SITE_URL || 'https://promptcompression.ai'       // content marketing
export const COMPANY_URL = import.meta.env.VITE_COMPANY_URL || 'https://quantecarlo.com'     // the optimizer and the company
export const DOCS_URL = import.meta.env.VITE_DOCS_URL || 'https://impromptune.com/docs'   // what the ⓘ hints link to
// Vite's base ('/studio/' or '/'); the API is mounted next to it by nginx and by app.main's dev prefix strip.
export const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')
// Normally the API sits next to the app, but on impromptune.com the app moved to /app/ while the API stayed at
// /api/ - the OAuth redirect URI registered with each provider names that path, and it is not ours to change.
export const API = import.meta.env.VITE_API_BASE || (BASE + '/api')
