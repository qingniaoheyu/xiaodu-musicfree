/**
 * 小度桥接源 —— MusicFree 插件
 *
 * 作用：把本地「小度桥服务」(bridge/server.py) 接进 MusicFree，
 *      并在播放时顺手把这首歌投到小度音箱（由桥服务的 gateway 后端完成，DLNA / MCP / 自建技能皆可）。
 *
 * 安装：MusicFree → 插件管理 → 从 URL / 本地文件安装本 js。
 * 配置：插件设置里填桥服务地址（如 http://192.168.1.10:8765）与可选 token。
 *
 * 协议依据：maotoumao/musicfree-skills → musicfree-plugin-dev → plugin-protocol.md
 */

const axios = require('axios');

const DEFAULT_BASE = 'http://127.0.0.1:8765';
const PAGE_SIZE = 30;

function getConf() {
    const v = (env.getUserVariables && env.getUserVariables()) || {};
    let base = (v.baseUrl || DEFAULT_BASE).trim();
    if (!/^https?:\/\//i.test(base)) {
        base = 'http://' + base;
    }
    base = base.replace(/\/+$/, '');
    return {
        base,
        token: (v.token || '').trim(),
        autoCast: v.autoCast !== 'false',
    };
}

function headersOf(token) {
    const h = { 'Content-Type': 'application/json' };
    if (token) {
        h['X-Bridge-Token'] = token;
    }
    return h;
}

async function api(path, params, conf) {
    const res = await axios.get(conf.base + path, {
        params: params || {},
        headers: headersOf(conf.token),
        timeout: 8000,
    });
    return res.data;
}

/** 桥服务返回的 track 已经接近 IMusicItem，这里补平台无关的兜底 */
function toMusicItem(raw) {
    return {
        id: String(raw.id),
        title: raw.title || '未知曲目',
        artist: raw.artist || '未知歌手',
        album: raw.album || '未知专辑',
        artwork: raw.artwork || undefined,
        duration: raw.duration || undefined,
        // 直链随搜索结果带回，getMediaSource 无法调用时兜底可用
        url: raw.url,
        // 扩展字段：供后面投送使用
        bridgePath: raw.url,
    };
}

module.exports = {
    platform: '小度桥接源',
    version: '0.1.0',
    author: 'xiaodu-musicfree',
    description: '本地曲库接入 MusicFree，播放时自动推送到小度音箱',
    cacheControl: 'no-store',
    supportedSearchType: ['music'],
    userVariables: [
        {
            key: 'baseUrl',
            name: '桥服务地址',
            hint: '例如 http://192.168.1.10:8765（小度音箱需要能访问到）',
        },
        {
            key: 'token',
            name: '访问令牌（可选）',
            hint: '若 bridge/config.json 里设置了 token，这里必须一致',
        },
        {
            key: 'autoCast',
            name: '播放时自动投送到小度',
            hint: '填 false 关闭，仅在本机播放',
        },
    ],
    hints: {
        search: ['搜索本地曲库，例如：周杰伦 或 晴天'],
    },

    async search(query, page, type) {
        if (type !== 'music') {
            return { isEnd: true, data: [] };
        }
        const conf = getConf();
        const p = Number(page) || 1;
        const data = await api(
            '/api/search',
            {
                q: query || '',
                limit: PAGE_SIZE,
                offset: (p - 1) * PAGE_SIZE,
            },
            conf,
        );
        const list = Array.isArray(data.data) ? data.data : [];
        return {
            isEnd: list.length < PAGE_SIZE,
            data: list.map(toMusicItem),
        };
    },

    async getMediaSource(musicItem, quality) {
        const conf = getConf();
        let url = musicItem.url || musicItem.bridgePath;

        // 直链可能带过期参数，重新向桥服务要一次
        try {
            const fresh = await api('/api/search', { q: musicItem.title || '', limit: 50, offset: 0 }, conf);
            const hit = (fresh.data || []).find((x) => String(x.id) === String(musicItem.id));
            if (hit && hit.url) {
                url = hit.url;
            }
        } catch (e) {
            // 拿不到就用缓存直链，别让播放整个失败
        }

        if (!url) {
            throw new Error('桥服务未返回可播放地址');
        }

        // 顺手投送到小度；失败不影响本机播放
        if (conf.autoCast) {
            try {
                await axios.post(
                    conf.base + '/api/cast',
                    {
                        id: musicItem.id,
                        title: musicItem.title,
                        artist: musicItem.artist,
                    },
                    { headers: headersOf(conf.token), timeout: 10000 },
                );
            } catch (e) {
                console.warn('投送到小度失败：' + (e && e.message));
            }
        }

        return { url };
    },

    async getLyric(musicItem) {
        const conf = getConf();
        try {
            const data = await api('/api/lyric/' + encodeURIComponent(musicItem.id), {}, conf);
            if (data && data.rawLrc) {
                return { rawLrc: data.rawLrc, translation: data.translation || undefined };
            }
        } catch (e) {
            // 没歌词不算错误
        }
        return { rawLrc: '' };
    },

    async getMusicInfo(musicItem) {
        return {};
    },
};
