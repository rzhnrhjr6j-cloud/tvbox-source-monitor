package com.github.catvod.crawler;

import android.content.Context;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import okhttp3.Dns;
import okhttp3.OkHttpClient;

/**
 * Minimal host-side copy of the TVBox crawler contract.
 *
 * Third-party spider jars are compiled against this class. Keeping the
 * signatures identical lets DexClassLoader load those jars inside the
 * verifier without pulling in the whole TVBox application.
 */
public class Spider {

    public static final JSONObject empty = new JSONObject();
    private static final OkHttpClient HTTP_CLIENT = new OkHttpClient();

    public String siteKey;
    protected static Context mContext;

    public void init(Context context) {
        mContext = context;
    }

    public void init(Context context, String extend) {
        init(context);
    }

    public void initApi(SpiderApi api) {
    }

    public String homeContent(boolean filter) {
        return "";
    }

    public String homeVideoContent() {
        return "";
    }

    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) {
        return "";
    }

    public String detailContent(List<String> ids) {
        return "";
    }

    public String searchContent(String key, boolean quick) {
        return "";
    }

    public String searchContent(String key, boolean quick, String pg) {
        return searchContent(key, quick);
    }

    public String playerContent(String flag, String id, List<String> vipFlags) {
        return "";
    }

    public boolean isVideoFormat(String url) {
        return false;
    }

    public boolean manualVideoCheck() {
        return false;
    }

    public String liveContent(String url) {
        return "";
    }

    public static Dns safeDns() {
        return Dns.SYSTEM;
    }

    public static OkHttpClient client() {
        return HTTP_CLIENT;
    }

    public void cancelByTag() {
    }

    public void destroy() {
    }

    public Object[] proxyLocal(Map<String, String> params) {
        return null;
    }

    public Object[] proxy(Map<String, String> params) {
        return proxyLocal(params);
    }

    public String action(String action) {
        return null;
    }
}
