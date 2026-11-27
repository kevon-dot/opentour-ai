import { configuredHandler } from '../../../lib/server/authkit-adapter.js';

export const runtime = 'nodejs';
export const POST = configuredHandler('signout');
