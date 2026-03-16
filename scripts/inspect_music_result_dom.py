#!/usr/bin/env python3
"""
使用 Chrome DevTools (CDP) 检查当前 Gemini 页面上已生成音乐的 DOM。
Connect to Chrome at localhost:9222, inspect current page for audio/video/media elements.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from playwright.async_api import async_playwright

CDP_URL = "http://localhost:9222"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            print("No browser context found")
            return
        context = contexts[0]
        pages = context.pages
        if not pages:
            print("No pages found")
            return

        print("Chrome 当前打开的 Gemini 页:")
        for i, p in enumerate(pages):
            u = p.url
            if "gemini.google.com" in u:
                print(f"  [{i+1}] {u}")
        print()

        # 支持: python inspect_music_result_dom.py [url|run_dir路径]  或  python inspect_music_result_dom.py --all
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        scan_all = "--all" in sys.argv
        target = args[0] if args else None

        target_url = None
        if target:
            run_dir = Path(target)
            if run_dir.is_dir() and (run_dir / "page_url.txt").exists():
                target_url = (run_dir / "page_url.txt").read_text(encoding="utf-8").strip()
                print(f"从 {run_dir} 读取 URL: {target_url}\n")
            else:
                target_url = target

        if target_url:
            for p in pages:
                if target_url in p.url or p.url in target_url:
                    page = p
                    break
            else:
                page = pages[-1]
                await page.goto(target_url, timeout=30000)
                await asyncio.sleep(3)
        elif scan_all:
            page = None  # 下面会遍历
        else:
            page = pages[-1]

        pages_to_check = [page] if page else pages
        if not page and not pages_to_check:
            print("No pages")
            return

        for idx, pg in enumerate(pages_to_check):
            if scan_all and len(pages_to_check) > 1:
                print(f"\n{'='*60}\n=== Tab {idx+1}: {pg.url[:80]}...\n{'='*60}")
            page = pg
            url = page.url
            if not scan_all:
                print(f"Current page URL: {url}\n")

            if "gemini.google.com" not in url and not scan_all:
                print("Warning: Not on Gemini. Current page may not have music result.")

            # 提取所有可能的音乐/音频相关 DOM
            result = await page.evaluate("""() => {
            const out = {
                url: window.location.href,
                audioElements: [],
                videoElements: [],
                mediaLinks: [],
                downloadLinks: [],
                mediaContainers: [],
                allMediaLike: [],
                downloadRelated: []
            };

            // 1. <audio> 元素
            document.querySelectorAll('audio').forEach((el, i) => {
                const src = el.src || (el.querySelector('source')?.src);
                out.audioElements.push({
                    index: i,
                    tag: 'audio',
                    src: src || null,
                    currentSrc: el.currentSrc || null,
                    outerHTML: el.outerHTML.slice(0, 500),
                    parentClass: el.parentElement?.className?.slice(0, 100)
                });
            });

            // 2. <video> 元素（音乐有时以 video 形式）
            document.querySelectorAll('video').forEach((el, i) => {
                const src = el.src || (el.querySelector('source')?.src);
                out.videoElements.push({
                    index: i,
                    tag: 'video',
                    src: src || null,
                    currentSrc: el.currentSrc || null,
                    outerHTML: el.outerHTML.slice(0, 500)
                });
            });

            // 3. source 元素
            document.querySelectorAll('audio source, video source').forEach((el, i) => {
                const src = el.src || el.getAttribute('src');
                if (src) {
                    out.mediaLinks.push({
                        type: el.parentElement?.tagName?.toLowerCase(),
                        src,
                        outerHTML: el.outerHTML.slice(0, 300)
                    });
                }
            });

            // 4. 带 .mp3/.wav/.m4a 的链接
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || a.getAttribute('href');
                if (href && (href.includes('.mp3') || href.includes('.wav') || href.includes('.m4a') || 
                    href.includes('audio') || a.download || a.getAttribute('download'))) {
                    out.downloadLinks.push({
                        href: href.slice(0, 200),
                        download: a.download || a.getAttribute('download'),
                        text: (a.innerText || a.textContent || '').trim().slice(0, 50),
                        outerHTML: a.outerHTML.slice(0, 400)
                    });
                }
            });

            // 5. 可能包含媒体播放器的容器
            const mediaSelectors = [
                '[class*="audio"]', '[class*="music"]', '[class*="player"]',
                '[class*="media"]', '[class*="playback"]', '[data-*="audio"]'
            ];
            mediaSelectors.forEach(sel => {
                try {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el.querySelector('audio, video, source') || el.innerText?.toLowerCase().includes('play')) {
                            out.mediaContainers.push({
                                selector: sel,
                                className: el.className?.slice(0, 80),
                                tag: el.tagName,
                                hasAudio: !!el.querySelector('audio'),
                                hasVideo: !!el.querySelector('video'),
                                innerHTML: el.innerHTML.slice(0, 300)
                            });
                        }
                    });
                } catch (e) {}
            });

            // 6. 全局搜索可能包含媒体 URL 的元素
            const allWithSrc = document.querySelectorAll('[src], [href]');
            allWithSrc.forEach(el => {
                const src = el.src || el.getAttribute('src') || el.href || el.getAttribute('href');
                if (src && typeof src === 'string' && 
                    (src.includes('.mp3') || src.includes('.wav') || src.includes('.m4a') || 
                     src.includes('blob:') || src.includes('audio') || src.includes('media'))) {
                    out.allMediaLike.push({
                        tag: el.tagName,
                        attr: el.src ? 'src' : 'href',
                        value: src.slice(0, 250),
                        outerHTML: el.outerHTML.slice(0, 400)
                    });
                }
            });

            // 7. iframe/embed/object
            document.querySelectorAll('iframe, embed, object').forEach((el) => {
                const s = el.src || el.getAttribute('src') || el.data || el.getAttribute('data');
                if (s) out.allMediaLike.push({ tag: el.tagName, type: 'embed', src: s.slice(0, 250), html: el.outerHTML.slice(0, 400) });
            });

            // 8. 含 play/download/播放/下载 的按钮
            document.querySelectorAll('button, [role="button"], a').forEach((el) => {
                const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').toLowerCase();
                const h = el.href || el.getAttribute('href') || '';
                if (t.includes('play') || t.includes('download') || t.includes('下载') || t.includes('播放') || h.includes('.mp3') || h.includes('.wav')) {
                    out.allMediaLike.push({ tag: el.tagName, text: t.slice(0, 50), href: h.slice(0, 200), html: el.outerHTML.slice(0, 500) });
                }
            });

            // 9. blob: URL (音频可能用 blob)
            document.querySelectorAll('*').forEach(el => {
                const s = el.src || el.getAttribute('src') || el.href || el.getAttribute('href');
                if (s && typeof s === 'string' && s.startsWith('blob:')) {
                    out.allMediaLike.push({ tag: el.tagName, blob: s.slice(0, 80), html: el.outerHTML.slice(0, 600) });
                }
            });

            // 10. 含 data-* 媒体相关
            document.querySelectorAll('[data-url], [data-src], [data-audio], [data-media]').forEach(el => {
                const d = el.getAttribute('data-url') || el.getAttribute('data-src') || el.getAttribute('data-audio') || el.getAttribute('data-media');
                if (d) out.allMediaLike.push({ tag: el.tagName, dataAttr: d.slice(0, 200), html: el.outerHTML.slice(0, 500) });
            });

            // 11. 类名含 playback 的元素及其父级整棵子树
            const playbackEls = document.querySelectorAll('[class*="playback"]');
            playbackEls.forEach((el, idx) => {
                const html = el.outerHTML;
                if (html.length < 2000) {
                    out.allMediaLike.push({ tag: el.tagName, className: el.className?.slice(0, 100), html });
                }
                // 找最近的有意义的父容器（含较多子节点），dump 其 HTML
                let p = el.parentElement;
                for (let i = 0; i < 8 && p; i++) {
                    const childCount = p.children?.length || 0;
                    if (childCount >= 1 && p.outerHTML.length < 3000) {
                        out.allMediaLike.push({ type: 'PLAYBACK_ANCESTOR', level: i, tag: p.tagName, className: p.className?.slice(0, 80), childCount, html: p.outerHTML.slice(0, 2500) });
                        break;
                    }
                    p = p.parentElement;
                }
            });

            // 12. 搜索所有 [src] 包括 blob:
            document.querySelectorAll('audio[src], video[src], source[src]').forEach(el => {
                const s = el.src || el.getAttribute('src');
                if (s) out.allMediaLike.push({ tag: el.tagName, mediaSrc: s.slice(0, 150), html: el.outerHTML.slice(0, 400) });
            });

            // 13. tts-control 及其内部完整结构（音乐/音频播放器）
            document.querySelectorAll('tts-control, [class*="tts-control"]').forEach((el, i) => {
                out.allMediaLike.push({ type: 'TTS_CONTROL', index: i, fullHTML: el.outerHTML });
            });

            // 14. 下载相关：Download、下载、save、保存、导出
            out.downloadRelated = [];
            const downloadKeywords = ['download', '下载', 'save', '保存', 'export', '导出', 'get', '获取'];
            document.querySelectorAll('button, [role="button"], a, [role="link"], [aria-label]').forEach(el => {
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const text = (el.innerText || el.textContent || '').toLowerCase();
                const href = el.href || el.getAttribute('href') || '';
                const hasDownload = el.download !== undefined || el.getAttribute('download');
                const match = downloadKeywords.some(k => aria.includes(k) || text.includes(k)) || hasDownload || href.includes('download');
                if (match) {
                    out.downloadRelated.push({
                        tag: el.tagName,
                        aria: el.getAttribute('aria-label'),
                        text: (el.innerText || el.textContent || '').trim().slice(0, 60),
                        href: href ? href.slice(0, 200) : null,
                        download: el.download || el.getAttribute('download'),
                        outerHTML: el.outerHTML.slice(0, 600)
                    });
                }
            });

            // 15. 含 download/save 图标的按钮（Material Icons: download, save_alt, file_download）
            document.querySelectorAll('mat-icon, [data-mat-icon-name], [class*="download"], [class*="save"]').forEach(el => {
                const name = (el.getAttribute('data-mat-icon-name') || el.getAttribute('fonticon') || el.className || '').toLowerCase();
                if (name.includes('download') || name.includes('save_alt') || name.includes('file_download') || name.includes('save')) {
                    const parent = el.closest('button, a, [role="button"]');
                    if (parent && !out.downloadRelated.some(d => d.parentHTML === (parent?.outerHTML?.slice(0, 200)))) {
                        out.downloadRelated.push({
                            tag: 'ICON_BUTTON',
                            iconName: name.slice(0, 50),
                            parentTag: parent?.tagName,
                            parentAria: parent?.getAttribute('aria-label'),
                            parentHTML: parent ? parent.outerHTML.slice(0, 600) : el.outerHTML.slice(0, 400)
                        });
                    }
                }
            });

            // 16. 含 Listen/播放 的按钮及其 data-* 属性
            document.querySelectorAll('[aria-label="Listen"], [aria-label*="Listen"], [aria-label*="Play"]').forEach(el => {
                const attrs = {};
                for (const a of el.attributes) attrs[a.name] = a.value;
                let p = el;
                for (let i = 0; i < 10 && p; i++) {
                    if (p.querySelector && p.querySelector('audio, video')) {
                        const media = p.querySelector('audio, video');
                        attrs._mediaSrc = media?.src || media?.querySelector('source')?.src;
                        break;
                    }
                    p = p.parentElement;
                }
                out.allMediaLike.push({ type: 'LISTEN_BUTTON', attrs, parentTag: el.parentElement?.tagName, html: el.closest('tts-control')?.outerHTML?.slice(0, 1500) });
            });

            return out;
        }""")

            total = (len(result.get("audioElements", [])) + len(result.get("videoElements", [])) +
                     len(result.get("mediaLinks", [])) + len(result.get("downloadLinks", [])) +
                     len(result.get("allMediaLike", [])))
            if scan_all and total == 0:
                print("(无媒体元素)")
                continue
            if scan_all and total > 0:
                print(f"*** 发现 {total} 个媒体相关元素 ***")

            print("=" * 60)
            print("=== <audio> 元素 ===")
            print("=" * 60)
            for x in result.get("audioElements", []):
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("audioElements"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== <video> 元素 ===")
            print("=" * 60)
            for x in result.get("videoElements", []):
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("videoElements"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== <source> 元素 (audio/video 内) ===")
            print("=" * 60)
            for x in result.get("mediaLinks", []):
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("mediaLinks"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== 下载链接 (含 .mp3/.wav/.m4a/audio/download) ===")
            print("=" * 60)
            for x in result.get("downloadLinks", []):
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("downloadLinks"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== 媒体相关容器 ===")
            print("=" * 60)
            for x in result.get("mediaContainers", [])[:10]:
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("mediaContainers"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== 所有含媒体 URL/播放/下载 的元素 ===")
            print("=" * 60)
            for x in result.get("allMediaLike", [])[:20]:
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("allMediaLike"):
                print("(无)")

            print("\n" + "=" * 60)
            print("=== 下载相关元素 (Download/下载/Save/保存/导出) ===")
            print("=" * 60)
            for x in result.get("downloadRelated", []):
                print(json.dumps(x, indent=2, ensure_ascii=False))
            if not result.get("downloadRelated"):
                print("(无)")


if __name__ == "__main__":
    asyncio.run(main())
