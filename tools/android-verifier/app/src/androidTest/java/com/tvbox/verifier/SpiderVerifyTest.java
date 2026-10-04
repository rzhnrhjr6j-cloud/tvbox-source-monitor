package com.tvbox.verifier;

import android.content.Context;
import android.os.Bundle;
import android.util.Log;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import dalvik.system.DexClassLoader;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs on a real (emulated) Android device and mirrors what the TVBox client
 * does when a source is opened: download the config, then for every crawler
 * (csp_*) site in it download the jar, DexClassLoader-load it, run Init.init,
 * instantiate the spider and walk the content path.
 *
 * The verdict is written to the app's external files dir so a host script can
 * pull it with adb.  Every site gets its own result object, because one config
 * commonly bundles twenty crawlers and a client only needs one of them to work
 * - the point of this tool is to say which ones actually do.
 */
@RunWith(AndroidJUnit4.class)
public class SpiderVerifyTest {

    private static final String TAG = "TVBoxVerify";
    private static final String DOMAIN = "com.github.catvod.spider.";
    private static final int MAX_SITES = 40;
    private static final int DEFAULT_CONNECT_TIMEOUT_MS = 10000;
    private static final int DEFAULT_READ_TIMEOUT_MS = 20000;

    private final JSONObject verdict = new JSONObject();
    private Context context;
    private int connectTimeoutMs = DEFAULT_CONNECT_TIMEOUT_MS;
    private int readTimeoutMs = DEFAULT_READ_TIMEOUT_MS;

    @Test
    public void verifySpider() {
        Bundle args = InstrumentationRegistry.getArguments();
        context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String configUrl = args.getString("configUrl", "");
        String spiderUrl = args.getString("spiderUrl", "");
        String spiderClass = args.getString("spiderClass", "");
        String onlySiteKey = args.getString("siteKey", "");
        String keyword = args.getString("keyword", "爱情");
        int maxSites = parseInt(args.getString("maxSites", ""), MAX_SITES);
        connectTimeoutMs = parseInt(
                args.getString("connectTimeoutMs", ""), DEFAULT_CONNECT_TIMEOUT_MS);
        readTimeoutMs = parseInt(
                args.getString("readTimeoutMs", ""), DEFAULT_READ_TIMEOUT_MS);
        String stage = "start";

        try {
            verdict.put("configUrl", configUrl);
            verdict.put("keyword", keyword);

            stage = "download_config";
            JSONObject config = null;
            if (!configUrl.isEmpty()) {
                config = new JSONObject(downloadText(configUrl));
            }
            if (config != null && spiderUrl.isEmpty()) {
                spiderUrl = config.optString("spider", "");
            }
            spiderUrl = stripMd5(spiderUrl);
            verdict.put("configSpider", spiderUrl);

            stage = "resolve_sites";
            List<JSONObject> sites = collectSpiderSites(config, onlySiteKey, maxSites);
            if (sites.isEmpty() && !spiderClass.isEmpty()) {
                // A single jar/class was passed in directly instead of a config.
                JSONObject synthetic = new JSONObject();
                synthetic.put("api", spiderClass);
                synthetic.put("key", spiderClass);
                synthetic.put("jar", spiderUrl);
                sites.add(synthetic);
            }
            verdict.put("spiderClassArg", spiderClass);
            verdict.put("siteCount", sites.size());
            if (sites.isEmpty()) {
                finish(false, stage, "NO_SPIDER_SITE");
                return;
            }

            JSONArray results = new JSONArray();
            boolean anyLoad = false;
            boolean anyPlay = false;
            boolean anyOk = false;
            for (JSONObject site : sites) {
                JSONObject result = verifySite(site, spiderUrl, keyword);
                results.put(result);
                anyLoad |= result.optBoolean("loadOk");
                anyPlay |= result.optBoolean("playOk");
                anyOk |= result.optBoolean("ok");
            }
            verdict.put("sites", results);
            verdict.put("anyLoadOk", anyLoad);
            verdict.put("anyPlayOk", anyPlay);
            stage = "done";
            finish(anyOk, stage, anyOk ? "" : "NO_PLAYABLE_SITE");
        } catch (Throwable error) {
            Log.e(TAG, "verification failed at " + stage, error);
            try {
                verdict.put("errorClass", error.getClass().getSimpleName());
            } catch (Exception ignored) {
                // best effort
            }
            finish(false, stage, String.valueOf(error.getMessage()));
        }
    }

    /**
     * Exercise one crawler site end to end and return its per-site verdict.
     *
     * The stages are ordered so that "the jar itself is broken" (the client's
     * "jar解析失败") is separated from "the jar loads but this site has nothing
     * to play right now".  Only a site that reaches a media URL counts as
     * playable, so a silently empty upstream never passes.
     */
    private JSONObject verifySite(JSONObject site, String configSpiderUrl, String keyword) {
        JSONObject result = new JSONObject();
        String stage = "site_start";
        String siteKey = site.optString("key", "");
        String spiderClass = site.optString("api", "");
        String jarUrl = stripMd5(site.optString("jar", ""));
        if (jarUrl.isEmpty()) {
            jarUrl = configSpiderUrl;
        }
        int searchable = site.optInt("searchable", 1);
        try {
            result.put("key", siteKey);
            result.put("spiderClass", spiderClass);
            result.put("jarUrl", jarUrl);
            result.put("searchable", searchable);
            if (jarUrl.isEmpty()) {
                result.put("loadOk", false);
                return failed(result, stage, "NO_JAR_URL");
            }
            if (spiderClass.isEmpty()) {
                result.put("loadOk", false);
                return failed(result, stage, "NO_SPIDER_CLASS");
            }

            stage = "download_jar";
            byte[] jar = downloadBytes(jarUrl);
            result.put("jarBytes", jar.length);
            File jarFile = new File(context.getCacheDir(), "spider-" + safeName(siteKey) + ".jar");
            try (FileOutputStream out = new FileOutputStream(jarFile)) {
                out.write(jar);
            }

            stage = "dex_loader";
            DexClassLoader loader = new DexClassLoader(
                    jarFile.getAbsolutePath(),
                    context.getCodeCacheDir().getAbsolutePath(),
                    null,
                    context.getClassLoader());

            stage = "init";
            result.put("initOk", callInit(loader));

            stage = "instantiate";
            Class<?> clazz = loader.loadClass(qualify(spiderClass));
            Object spider = clazz.getDeclaredConstructor().newInstance();
            callSpiderInit(clazz, spider, site);
            result.put("instantiateOk", true);
            // Past this point the jar loads and runs on this device, so the
            // config's own reference is good; loadOk stays true even if the
            // upstream content is empty.
            result.put("loadOk", true);

            JSONArray items = null;
            if (searchable > 0) {
                stage = "search";
                String searchRaw = asText(invoke(clazz, spider, "searchContent",
                        new Class<?>[]{String.class, boolean.class}, new Object[]{keyword, false}));
                JSONObject search = new JSONObject(searchRaw);
                items = search.optJSONArray("list");
            }
            int itemCount = items == null ? 0 : items.length();
            result.put("searchOk", itemCount > 0);
            result.put("searchCount", itemCount);
            if (itemCount == 0) {
                // Not searchable, or search returned nothing: fall back to the
                // catalog list so a browse-only site can still be judged.
                stage = "home";
                JSONObject home = new JSONObject(asText(invoke(clazz, spider, "homeContent",
                        new Class<?>[]{boolean.class}, new Object[]{true})));
                JSONArray homeList = home.optJSONArray("list");
                result.put("homeListCount", homeList == null ? 0 : homeList.length());
                if (homeList != null && homeList.length() > 0) {
                    items = homeList;
                    itemCount = homeList.length();
                }
            }
            if (itemCount == 0) {
                return failed(result, stage, "NO_ITEM");
            }
            String vodId = items.optJSONObject(0).optString("vod_id", "");
            result.put("firstId", vodId);

            stage = "detail";
            List<String> ids = new ArrayList<>();
            ids.add(vodId);
            JSONObject detail = new JSONObject(asText(invoke(clazz, spider, "detailContent",
                    new Class<?>[]{List.class}, new Object[]{ids})));
            JSONArray detailList = detail.optJSONArray("list");
            boolean detailOk = detailList != null && detailList.length() > 0;
            result.put("detailOk", detailOk);
            if (!detailOk) {
                return failed(result, stage, "DETAIL_EMPTY");
            }
            String playUrl = firstPlayUrl(detailList.optJSONObject(0));
            result.put("playlistUrl", playUrl);

            stage = "play";
            if (!playUrl.isEmpty()) {
                JSONObject play = new JSONObject(asText(invoke(clazz, spider, "playerContent",
                        new Class<?>[]{String.class, String.class, List.class},
                        new Object[]{siteKey, playUrl, new ArrayList<String>()})));
                String mediaUrl = play.optString("url", "");
                result.put("playUrl", mediaUrl);
                if (mediaUrl.isEmpty() || !mediaUrl.startsWith("http")) {
                    result.put("playOk", false);
                    return failed(result, stage, "PLAY_URL_NOT_HTTP");
                }
                stage = "media_probe";
                MediaProbe probe = probeMedia(mediaUrl, play.optJSONObject("header"));
                result.put("playOk", probe.ok);
                result.put("mediaStatusCode", probe.statusCode);
                result.put("mediaContentType", probe.contentType);
                if (!probe.ok) {
                    return failed(result, stage, probe.error);
                }
            } else {
                result.put("playOk", false);
                return failed(result, stage, "PLAY_EMPTY");
            }
            boolean playOk = result.optBoolean("playOk");
            result.put("ok", playOk);
            result.put("stage", playOk ? "done" : "play");
            result.put("error", playOk ? "" : "PLAY_EMPTY");
            return result;
        } catch (Throwable error) {
            Log.w(TAG, "site " + siteKey + " failed at " + stage, error);
            try {
                result.put("errorClass", error.getClass().getSimpleName());
            } catch (Exception ignored) {
                // best effort
            }
            return failed(result, stage, String.valueOf(error.getMessage()));
        }
    }

    private static JSONObject failed(JSONObject result, String stage, String error) {
        try {
            result.put("ok", false);
            result.put("stage", stage);
            result.put("error", error == null ? "" : error);
        } catch (Exception ignored) {
            // best effort
        }
        return result;
    }

    private static List<JSONObject> collectSpiderSites(
            JSONObject config, String onlySiteKey, int maxSites) {
        List<JSONObject> found = new ArrayList<>();
        if (config == null) {
            return found;
        }
        JSONArray sites = config.optJSONArray("sites");
        if (sites == null) {
            return found;
        }
        for (int i = 0; i < sites.length() && found.size() < Math.max(1, maxSites); i++) {
            JSONObject item = sites.optJSONObject(i);
            if (item == null) {
                continue;
            }
            String api = item.optString("api", "");
            if (!isSpiderSite(api)) {
                continue;
            }
            if (!onlySiteKey.isEmpty() && !onlySiteKey.equals(item.optString("key", ""))) {
                continue;
            }
            found.add(item);
        }
        return found;
    }

    private void finish(boolean ok, String stage, String error) {
        try {
            verdict.put("ok", ok);
            verdict.put("stage", stage);
            verdict.put("error", error == null ? "" : error);
            File dir = context.getExternalFilesDir(null);
            File out = new File(dir, "verify-result.json");
            try (FileOutputStream stream = new FileOutputStream(out)) {
                stream.write(verdict.toString(2).getBytes(StandardCharsets.UTF_8));
            }
            Log.i(TAG, "verdict written: ok=" + ok + " stage=" + stage);
        } catch (Exception writeError) {
            Log.e(TAG, "could not write verdict", writeError);
        }
    }

    private static boolean isSpiderSite(String api) {
        return api.startsWith("csp_")
                || api.startsWith("Csp_")
                || (api.indexOf('.') < 0 && !api.isEmpty() && !api.startsWith("http"));
    }

    private static String qualify(String name) {
        if (name.indexOf('.') >= 0) {
            return name;
        }
        if (name.startsWith("csp_") || name.startsWith("Csp_")) {
            name = name.substring(4);
        }
        return DOMAIN + name;
    }

    private static String safeName(String name) {
        return name.replaceAll("[^A-Za-z0-9_\\-]", "_");
    }

    private static String stripMd5(String url) {
        int cut = url.indexOf(';');
        return cut >= 0 ? url.substring(0, cut) : url;
    }

    private boolean callInit(DexClassLoader loader) {
        Context initContext = context.getApplicationContext();
        try {
            Class<?> init = loader.loadClass("com.github.catvod.spider.Init");
            try {
                Method method = init.getMethod("init", Context.class);
                method.invoke(null, initContext);
                return true;
            } catch (NoSuchMethodException missing) {
                Method method = init.getMethod("init", Context.class, String.class);
                method.invoke(null, initContext, "");
                return true;
            }
        } catch (java.lang.reflect.InvocationTargetException error) {
            Throwable cause = error.getCause() == null ? error : error.getCause();
            Log.w(TAG, "Init not invoked: " + cause, cause);
            return false;
        } catch (Throwable error) {
            Log.w(TAG, "Init not invoked: " + error);
            return false;
        }
    }

    private void callSpiderInit(Class<?> clazz, Object spider, JSONObject site) {
        Context initContext = context.getApplicationContext();
        String ext = "";
        try {
            ext = site == null ? "" : site.optString("ext", "");
        } catch (Throwable ignored) {
            // optString can throw on a non-string ext; empty is a safe default
        }
        try {
            Method method = clazz.getMethod("init", Context.class, String.class);
            method.invoke(spider, initContext, ext);
            return;
        } catch (NoSuchMethodException ignored) {
            // try the next signature
        } catch (Throwable error) {
            Log.w(TAG, "init(Context, ext) failed: " + error);
        }
        try {
            Method method = clazz.getMethod("init", Context.class);
            method.invoke(spider, initContext);
        } catch (Throwable error) {
            Log.w(TAG, "init(Context) failed: " + error);
        }
    }

    private static Object invoke(Class<?> clazz, Object target, String name, Class<?>[] types, Object[] args) {
        try {
            Method method = clazz.getMethod(name, types);
            method.setAccessible(true);
            return method.invoke(target, args);
        } catch (Throwable error) {
            Log.w(TAG, name + " failed: " + error);
            return null;
        }
    }

    private static String asText(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    private static String firstPlayUrl(JSONObject detail) {
        if (detail == null) {
            return "";
        }
        String playUrl = detail.optString("vod_play_url", "");
        if (playUrl.isEmpty()) {
            return "";
        }
        String[] episodes = playUrl.split("#");
        if (episodes.length == 0) {
            return "";
        }
        String[] parts = episodes[0].split("\\$");
        return parts.length > 1 ? parts[1] : parts[0];
    }

    private String downloadText(String url) throws Exception {
        return new String(downloadBytes(url), StandardCharsets.UTF_8);
    }

    private byte[] downloadBytes(String url) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(connectTimeoutMs);
        connection.setReadTimeout(readTimeoutMs);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestProperty("User-Agent", "okhttp/4.12.0");
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                throw new IllegalStateException("HTTP " + status + " for " + url);
            }
            try (InputStream stream = connection.getInputStream();
                 ByteArrayOutputStream buffer = new ByteArrayOutputStream()) {
                byte[] chunk = new byte[16384];
                int read;
                while ((read = stream.read(chunk)) > 0) {
                    buffer.write(chunk, 0, read);
                }
                return buffer.toByteArray();
            }
        } finally {
            connection.disconnect();
        }
    }

    private MediaProbe probeMedia(String url, JSONObject headers) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setConnectTimeout(connectTimeoutMs);
            connection.setReadTimeout(readTimeoutMs);
            connection.setInstanceFollowRedirects(true);
            connection.setRequestProperty("User-Agent", "okhttp/4.12.0");
            connection.setRequestProperty("Accept", "*/*");
            if (headers != null) {
                java.util.Iterator<String> keys = headers.keys();
                while (keys.hasNext()) {
                    String name = keys.next();
                    String value = headers.optString(name, "");
                    if (!name.isEmpty() && !value.isEmpty()) {
                        connection.setRequestProperty(name, value);
                    }
                }
            }
            int status = connection.getResponseCode();
            String contentType = String.valueOf(connection.getContentType());
            if (status < 200 || status >= 400) {
                return new MediaProbe(false, status, contentType, "MEDIA_HTTP_" + status);
            }
            try (InputStream stream = connection.getInputStream()) {
                int firstByte = stream.read();
                if (firstByte < 0) {
                    return new MediaProbe(false, status, contentType, "MEDIA_EMPTY");
                }
            }
            return new MediaProbe(true, status, contentType, "");
        } catch (Throwable error) {
            return new MediaProbe(false, 0, "", String.valueOf(error.getMessage()));
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static final class MediaProbe {
        final boolean ok;
        final int statusCode;
        final String contentType;
        final String error;

        MediaProbe(boolean ok, int statusCode, String contentType, String error) {
            this.ok = ok;
            this.statusCode = statusCode;
            this.contentType = contentType == null ? "" : contentType;
            this.error = error == null ? "" : error;
        }
    }

    private static int parseInt(String value, int fallback) {
        try {
            return Integer.parseInt(value);
        } catch (Throwable ignored) {
            return fallback;
        }
    }
}
