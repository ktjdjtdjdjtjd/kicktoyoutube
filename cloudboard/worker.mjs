import html from './index.html';
import {handle} from './api.mjs';
export default {fetch: (request, env) => handle(request, env, html)};
