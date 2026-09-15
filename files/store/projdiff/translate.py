#!/usr/bin/env python3
"""用 AI 把待翻译工单翻一版草稿（只出草稿文件，绝不写进工程）。

为什么做这个
  查出三个工程的俄语/哈萨克语缺 126~129 条翻译，用户会看到中文。
  人工翻 750 条太慢，先让 AI 出一版草稿给人审。

安全边界（这块最容易出事）
  · **绝不直接写进工程的 .lproj** —— 只输出到本功能目录下的草稿文件
  · 草稿是 .strings 格式但**放在草稿目录**，人审完自己复制过去
  · 术语表固定（运单/网点/签收这类物流术语），避免翻得五花八门
  · 每条都保留中文原文和英文参考，方便对照
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(SCRIPT_DIR)
OUT = os.path.join(HOME, "drafts")
PLATFORM = "http://127.0.0.1:8880"
WORKLIST = os.path.expanduser("~/Desktop/工作平台/待翻译工单.json")

# 物流行业术语表 —— 固定译法，别让 AI 每次翻得不一样
GLOSSARY = {
    "ru": {
        "运单": "накладная", "运单号": "номер накладной",
        "网点": "пункт обслуживания", "签收": "подтверждение получения",
        "派送": "доставка", "收件人": "получатель", "寄件人": "отправитель",
        "快递员": "курьер", "扫码": "сканирование", "扫描": "сканирование",
        "异常": "исключение", "问题件": "проблемное отправление",
        "取消": "Отмена", "确定": "OK", "保存": "Сохранить",
        "重试": "Повторить", "网络": "сеть", "加载中": "Загрузка",
        "暂无数据": "Нет данных", "合计": "Итого", "保价": "страховая стоимость",
        "运费": "стоимость доставки", "重量": "вес", "体积": "объём",
    },
    "kk-KZ": {
        "运单": "жүкқұжат", "运单号": "жүкқұжат нөмірі",
        "网点": "қызмет көрсету пункті", "签收": "қабылдауды растау",
        "派送": "жеткізу", "收件人": "алушы", "寄件人": "жіберуші",
        "快递员": "курьер", "扫码": "сканерлеу", "扫描": "сканерлеу",
        "异常": "ерекше жағдай", "取消": "Болдырмау", "确定": "OK",
        "保存": "Сақтау", "重试": "Қайталау", "网络": "желі",
        "加载中": "Жүктелуде", "暂无数据": "Деректер жоқ", "合计": "Барлығы",
    },
}


def ask_ai(prompt, timeout=600):
    req = urllib.request.Request(
        PLATFORM + "/api/ai/chat",
        data=json.dumps({"q": prompt}).encode(),
        headers={"Content-Type": "application/json"})
    out = ""
    for raw in urllib.request.urlopen(req, timeout=timeout):
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        if ev.get("type") == "text":
            out += ev["text"]
        elif ev.get("type") == "done":
            break
    return out


LANG_NAME = {"ru": "俄语", "kk-KZ": "哈萨克语", "ar": "阿拉伯语",
             "fr": "法语", "en": "英语", "zh-Hans": "简体中文",
             "de_DE": "德语", "es_ES": "西班牙语", "it_IT": "意大利语"}


def translate_batch(rows, proj, lang, batch=40):
    """把一批 key 交给 AI 翻译。返回 {key: 译文}"""
    name = LANG_NAME.get(lang, lang)
    gloss = GLOSSARY.get(lang, {})
    result = {}
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        lines = []
        for r in chunk:
            en = (r.get("英文参考") or "").strip()
            lines.append("- %s%s" % (r["key"], ("   [英文参考: %s]" % en) if en else ""))
        prompt = (
            "你是专业的 App 本地化翻译。把下面这些 iOS 界面文案翻译成**%s**。\n\n"
            "要求：\n"
            "1. **只输出 `原文|译文` 一行一条**，不要编号、不要解释、不要 Markdown 表格\n"
            "2. 行数和顺序与输入完全一致\n"
            "3. 这是物流/快递类 App，"
            "术语要统一：\n%s\n"
            "4. 占位符（%%@、%%d、{0}）原样保留，位置不要动\n"
            "5. 短标签（如按钮文字）尽量短\n"
            "6. `key` 里如果有 `_gh`/`_kz` 这类后缀，说明是特定国家的版本，照常翻\n\n"
            "要翻译的（共 %d 条）：\n%s"
            % (name, "\n".join("   %s = %s" % (k, v) for k, v in gloss.items()),
               len(chunk), "\n".join(lines)))
        try:
            out = ask_ai(prompt)
        except Exception as exc:
            print("    ⚠️ 第 %d 批失败：%s" % (i // batch + 1, exc))
            continue
        # 解析 "原文|译文"
        got = 0
        for line in out.splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if "|" not in line:
                continue
            k, _, v = line.partition("|")
            k, v = k.strip().strip('"'), v.strip().strip('"')
            if k in {r["key"] for r in chunk} and v:
                result[k] = v
                got += 1
        print("    第 %d/%d 批：拿到 %d/%d 条"
              % (i // batch + 1, (len(rows) + batch - 1) // batch, got, len(chunk)))
        time.sleep(0.4)
    return result


def safe_strings_line(key, val):
    """生成一行 .strings。转义引号和换行，别弄出非法文件。"""
    k = key.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    v = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"%s" = "%s";' % (k, v)


def main():
    if not os.path.isfile(WORKLIST):
        print("  ❌ 找不到工单：%s" % WORKLIST)
        print("     （先用「工程对比」或生成脚本产出待翻译工单.json）")
        return 1
    rows = json.load(open(WORKLIST, encoding="utf-8"))
    # 只翻真正需要的语言（zh-Hans 不用翻 —— key 本身就是中文）
    todo = {}
    for r in rows:
        if r["目标语言"] in ("zh-Hans",):
            continue
        todo.setdefault((r["工程"], r["目标语言"]), []).append(r)

    os.makedirs(OUT, exist_ok=True)
    print("  要翻 %d 组，共 %d 条"
          % (len(todo), sum(len(v) for v in todo.values())))
    print()
    summary = []
    for (proj, lang), items in sorted(todo.items(), key=lambda x: -len(x[1])):
        print("  【%s → %s】%d 条" % (proj, lang, len(items)))
        got = translate_batch(items, proj, lang)
        if not got:
            print("    ❌ 一条都没拿到，跳过")
            continue
        # 写草稿（**不碰工程**）
        lines = [
            "/* %s 的 %s 翻译草稿 —— AI 生成，**需要人审**" % (proj, LANG_NAME.get(lang, lang)),
            "   生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "   共 %d 条（原缺 %d 条）" % (len(got), len(items)),
            "",
            "   怎么用：审完之后，把要采纳的行复制到工程的",
            "   Logistics/Supporting Files/%s.lproj/Localizable.strings 末尾。" % lang,
            "   **这个文件本身不会自动写进任何工程。**",
            "*/",
            "",
        ]
        missing = [r for r in items if r["key"] not in got]
        for r in items:
            if r["key"] in got:
                lines.append("// 原：%s%s" % (
                    r["key"][:60],
                    ("   英：%s" % r["英文参考"][:50]) if r.get("英文参考") else ""))
                lines.append(safe_strings_line(r["key"], got[r["key"]]))
                lines.append("")
        if missing:
            lines.append("/* ── 下面这些 AI 没翻出来，需要人工补 ── */")
            for r in missing:
                lines.append("// %s" % r["key"][:70])
        path = os.path.join(OUT, "%s_%s.strings" % (proj, lang))
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        # 验证合法性 —— **用 plutil，不要用 plistlib**。
        # 踩过的坑：plistlib 解析不了 .strings 的旧式 ASCII 格式，
        # 连用户工程里正在用的文件它都说"Invalid file"。
        # plutil 是 macOS 自带的、Xcode 同款解析器，才是对的验证工具。
        ok = True
        try:
            import subprocess as _sp
            r = _sp.run(["plutil", "-lint", path], capture_output=True,
                        text=True, timeout=60)
            if r.returncode != 0 or "OK" not in (r.stdout + r.stderr):
                ok = False
                print("    ⚠️ plutil 说格式有问题：%s"
                      % (r.stdout + r.stderr).strip()[:80])
        except Exception as exc:
            ok = None            # 验不了（没有 plutil），不当作失败
            print("    （跳过格式验证：%s）" % str(exc)[:50])
        print("    ✅ %d/%d 条 → %s%s"
              % (len(got), len(items), os.path.basename(path),
                 "（plist 合法）" if ok else "（⚠️ 格式有问题）"))
        summary.append({"proj": proj, "lang": lang, "got": len(got),
                        "total": len(items), "file": path, "valid": ok})
    print()
    print("  ── 汇总 ──")
    tot_got = sum(s["got"] for s in summary)
    tot_all = sum(s["total"] for s in summary)
    print("  翻出 %d/%d 条，草稿在 %s" % (tot_got, tot_all, OUT))
    for s in summary:
        print("    %-16s %-8s %4d/%4d  %s" % (s["proj"], s["lang"], s["got"],
              s["total"], "✅" if s["valid"] else "⚠️"))
    print()
    print("  ⚠️ 这些是 **AI 草稿，没有写进任何工程**。")
    print("     审完自己复制到工程的 .lproj 里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
