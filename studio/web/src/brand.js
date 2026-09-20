// The studio's name and the sites it links to. Set at build time; nothing else in web/ hard-codes them.
// The name is a placeholder until the domain is final, so it lives in one place.
export const NAME = import.meta.env.VITE_STUDIO_NAME || 'Impromptune'
export const SITE_URL = import.meta.env.VITE_SITE_URL || 'https://promptcompression.ai'       // content marketing
export const COMPANY_URL = import.meta.env.VITE_COMPANY_URL || 'https://quantecarlo.com'     // the optimizer and the company
// Vite's base ('/studio/' or '/'); the API is mounted next to it by nginx and by app.main's dev prefix strip.
export const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')
export const API = BASE + '/api'
