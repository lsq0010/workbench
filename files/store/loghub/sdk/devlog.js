/**
 * devlog.js —— uni-app / 小程序 / H5 用的日志 SDK
 *
 * 用法：
 *   import DevLog from '@/utils/devlog.js'
 *   DevLog.start('192.168.0.104', 8918, 'SpeedafUniapp')
 *   DevLog.i('首页加载完成')                          // 只记内容
 *   DevLog.i('首页加载完成', 'Home')                   // 第二个参数是标签
 *   DevLog.e('提交失败', { code: 500, url: '/x' })     // 第二个参数是对象 = 上下文
 *   DevLog.e('提交失败', 'Net', { code: 500 })         // 标签 + 上下文
 */
const DevLog = {
  cfg: null, pending: [], timer: null, dropped: 0, maxPending: 2000,

  start(host, port = 8918, tag = 'App', minLevel = 'debug') {
    this.cfg = { host, port, tag, minLevel };
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => this.flush(), 1500);
    this.i('DevLog 已连接 ' + host + ':' + port, 'DevLog');
    return true;
  },
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; this.cfg = null; },

  _rank: { debug: 0, info: 1, warn: 2, error: 3, fatal: 4 },
  _write(level, msg, tag, extra) {
    if (!this.cfg) return;
    if (this._rank[level] < this._rank[this.cfg.minLevel]) return;
    this.pending.push({
      level, tag: tag || this.cfg.tag, msg: String(msg),
      device: (typeof uni !== 'undefined' && uni.getSystemInfoSync)
        ? (uni.getSystemInfoSync().model || '') : 'H5',
      ts: this._ts(), extra: extra || null,
    });
    if (this.pending.length > this.maxPending) {
      const d = this.pending.length - this.maxPending;
      this.pending.splice(0, d); this.dropped += d;
    }
    if (level === 'fatal' || this.pending.length >= 30) this.flush();
  },
  _ts() {
    const d = new Date(), p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ` +
           `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.` +
           String(d.getMilliseconds()).padStart(3, '0');
  },
  flush() {
    if (!this.cfg || !this.pending.length) return;
    const batch = this.pending.slice(); this.pending = [];
    if (this.dropped) {
      batch.push({ level: 'warn', tag: 'DevLog', ts: this._ts(),
                   msg: `⚠️ 之前有 ${this.dropped} 条日志因断网太久被丢弃` });
      this.dropped = 0;
    }
    const url = `http://${this.cfg.host}:${this.cfg.port}/api/log`;
    const body = JSON.stringify({ source: 'sdk-js', logs: batch });
    // uni.request / fetch 都试
    if (typeof uni !== 'undefined' && uni.request) {
      uni.request({ url, method: 'POST', data: { source: 'sdk-js', logs: batch },
        header: { 'Content-Type': 'application/json' },
        fail: () => { this.pending.unshift(...batch); } });
    } else if (typeof fetch !== 'undefined') {
      fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body }).catch(() => { this.pending.unshift(...batch); });
    }
  },
  // 第二个参数既可能是标签（字符串）也可能是上下文（对象）——
  // JS 没有参数名，所以靠类型判断。不这么做的话
  // DevLog.e('提交失败', {code:500}) 会把那个对象当成标签。
  _split(t, e) {
    if (t && typeof t === 'object') return [null, t];
    return [t, e];
  },
  d(m, t, e) { const [a, b] = this._split(t, e); this._write('debug', m, a, b); },
  i(m, t, e) { const [a, b] = this._split(t, e); this._write('info', m, a, b); },
  w(m, t, e) { const [a, b] = this._split(t, e); this._write('warn', m, a, b); },
  e(m, t, e) { const [a, b] = this._split(t, e); this._write('error', m, a, b); },
  fatal(m, t, e) { const [a, b] = this._split(t, e); this._write('fatal', m, a, b); },
};
export default DevLog;
