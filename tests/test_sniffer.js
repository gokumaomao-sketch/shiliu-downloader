const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const stub = new Proxy(() => {}, { get: () => stub });
let responseFilter;
const chrome = Object.create(stub);
chrome.webRequest = Object.create(stub);
chrome.webRequest.onHeadersReceived = { addListener: (_handler, filter) => { responseFilter = filter; } };
const context = vm.createContext({ chrome, navigator: { userAgent: 'Mozilla/5.0' }, URL });
vm.runInContext(fs.readFileSync('browser_extension/background.js', 'utf8'), context);
const classify = (url, mime) => vm.runInContext(`classify(${JSON.stringify(url)}, ${JSON.stringify(mime)})`, context);
assert.equal(responseFilter.urls[0], '<all_urls>');
assert.equal(classify('https://example.com/media?id=1', 'video/mp4').kind, 'file');
assert.equal(classify('https://example.com/playlist?id=1', 'application/vnd.apple.mpegurl').kind, 'hls');
assert.equal(classify('https://example.com/manifest?id=1', 'application/dash+xml').kind, 'dash');
assert.equal(classify('https://example.com/watch', 'text/html'), null);
vm.runInContext(`tabMedia.set(1, new Map([
  ['__page__', {kind:'page',mclass:'video'}],
  ['audio', {kind:'file',mclass:'audio'}],
  ['video', {kind:'file',mclass:'video'}]
]));`, context);
assert.equal(vm.runInContext('mediaList(1).length', context), 2);
vm.runInContext(`tabMedia.set(2, new Map([
  ['__page__', {kind:'page',mclass:'video',pageUrl:'https://www.xinpianchang.com/a123',title:'影片标题'}],
  ['clip', {kind:'file',mclass:'video',pageUrl:'https://www.xinpianchang.com/a123',size:39700000,ext:'mp4',name:'random-guid.mp4',title:'影片标题'}]
]));`, context);
assert.equal(vm.runInContext('mediaList(2)[0].kind', context), 'page');
assert.equal(vm.runInContext('mediaList(2).length', context), 2);
assert.equal(vm.runInContext('recommendedDetected(2, "https://www.xinpianchang.com/a123").kind', context), 'file');
assert.equal(vm.runInContext('filenameForMedia(mediaList(2)[1], {})', context), '影片标题.mp4');
