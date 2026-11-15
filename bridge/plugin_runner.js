/**
 * MusicFree 插件运行时（供 Python 桥服务调用）
 *
 * 用法：
 *   node plugin_runner.js <插件路径> <命令> <JSON参数> [环境变量JSON]
 *
 * 命令：
 *   search  {"query":"晴天","page":1,"type":"music"}
 *   source  {"item":{...},"quality":"standard"}
 *   lyric   {"item":{...}}
 *   info    {}
 *
 * 输出：单行 JSON 到 stdout，形如 {"ok":true,"data":...} 或 {"ok":false,"error":"..."}
 *
 * 说明：这里只负责按 MusicFree 插件协议调用插件的方法，
 * 不包含任何风控绕过逻辑；插件自身行为与 MusicFree 客户端一致。
 */

'use strict';

const fs = require('fs');
const path = require('path');

// 让插件能 require 到依赖：优先 bridge/node_modules，其次 ../vendor/node_modules
const nodeModules = (() => {
    const here = path.resolve(__dirname, 'node_modules');
    if (fs.existsSync(here)) return here;
    const alt = path.resolve(__dirname, '..', 'vendor', 'node_modules');
    if (fs.existsSync(alt)) return alt;
    return here;
})();

function logErr(...args) {
    process.stderr.write(args.map(String).join(' ') + '\n');
}

function out(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}

// 插件可用的模块白名单（与 MusicFree 一致）
function makeRequire() {
    return function req(name) {
        switch (name) {
            case 'axios':
                return require(path.join(nodeModules, 'axios'));
            case 'cheerio':
                return require(path.join(nodeModules, 'cheerio'));
            case 'crypto-js':
                return require(path.join(nodeModules, 'crypto-js'));
            case 'dayjs':
                return require(path.join(nodeModules, 'dayjs'));
            case 'big-integer':
                return require(path.join(nodeModules, 'big-integer'));
            case 'qs':
                return require(path.join(nodeModules, 'qs'));
            case 'he':
                return require(path.join(nodeModules, 'he'));
            default:
                // 未在白名单的模块直接报错，避免插件静默失败
                throw new Error('模块不可用: ' + name);
        }
    };
}

async function main() {
    const [, , pluginPath, command, argsJson, envJson] = process.argv;

    if (!pluginPath || !command) {
        out({ ok: false, error: '用法: node plugin_runner.js <插件路径> <命令> [JSON参数] [环境变量JSON]' });
        process.exit(2);
    }

    let args = {};
    if (argsJson) {
        try {
            args = JSON.parse(argsJson);
        } catch (e) {
            out({ ok: false, error: '参数 JSON 解析失败: ' + e.message });
            process.exit(2);
        }
    }

    let userVars = {};
    if (envJson) {
        try {
            userVars = JSON.parse(envJson) || {};
        } catch (e) {
            userVars = {};
        }
    }

    if (!fs.existsSync(pluginPath)) {
        out({ ok: false, error: '插件文件不存在: ' + pluginPath });
        process.exit(2);
    }

    const code = fs.readFileSync(pluginPath, 'utf8');
    const mod = { exports: {} };

    const env = {
        getUserVariables: () => userVars,
        os: process.platform,
        appVersion: '1.0.0',
        lang: 'zh-CN',
    };

    // 插件可能引用 window 等浏览器对象，给最小垫片
    if (typeof globalThis.window === 'undefined') {
        globalThis.window = globalThis;
    }

    let plugin;
    try {
        const fn = new Function('require', 'module', 'exports', 'env', 'console', 'global', code);
        fn(makeRequire(), mod, mod.exports, env, console, globalThis);
        plugin = mod.exports.default || mod.exports;
    } catch (e) {
        out({ ok: false, error: '插件加载失败: ' + e.message });
        process.exit(1);
    }

    if (!plugin || typeof plugin !== 'object') {
        out({ ok: false, error: '插件未导出对象' });
        process.exit(1);
    }

    try {
        switch (command) {
            case 'info': {
                out({
                    ok: true,
                    data: {
                        platform: plugin.platform || '',
                        version: plugin.version || '',
                        author: plugin.author || '',
                        description: plugin.description || '',
                        supportedSearchType: plugin.supportedSearchType || [],
                        userVariables: (plugin.userVariables || []).map((v) => ({
                            key: v.key,
                            name: v.name || v.key,
                            hint: v.hint || '',
                        })),
                        methods: ['search', 'getMediaSource', 'getLyric', 'getMusicInfo', 'getAlbumInfo',
                            'getMusicSheetInfo', 'getArtistWorks', 'getTopLists', 'getTopListDetail',
                            'getRecommendSheetTags', 'getRecommendSheetsByTag', 'importMusicItem', 'importMusicSheet'
                        ].filter((m) => typeof plugin[m] === 'function'),
                    },
                });
                return;
            }
            case 'search': {
                if (typeof plugin.search !== 'function') {
                    out({ ok: false, error: '插件未实现 search' });
                    return;
                }
                const res = await plugin.search(args.query || '', args.page || 1, args.type || 'music');
                out({ ok: true, data: res || { isEnd: true, data: [] } });
                return;
            }
            case 'source': {
                if (typeof plugin.getMediaSource !== 'function') {
                    // 没实现就回退到 item.url
                    const u = (args.item && args.item.url) || '';
                    out({ ok: !!u, data: { url: u }, error: u ? undefined : '插件未实现 getMediaSource 且 item 无 url' });
                    return;
                }
                const res = await plugin.getMediaSource(args.item || {}, args.quality || 'standard');
                out({ ok: true, data: res || {} });
                return;
            }
            case 'lyric': {
                if (typeof plugin.getLyric !== 'function') {
                    out({ ok: false, error: '插件未实现 getLyric' });
                    return;
                }
                const res = await plugin.getLyric(args.item || {});
                out({ ok: true, data: res || {} });
                return;
            }
            default:
                out({ ok: false, error: '未知命令: ' + command });
                process.exit(2);
        }
    } catch (e) {
        out({ ok: false, error: '调用失败: ' + (e && e.message ? e.message : String(e)) });
        process.exit(1);
    }
}

main().catch((e) => {
    out({ ok: false, error: '未捕获异常: ' + (e && e.message ? e.message : String(e)) });
    process.exit(1);
});
