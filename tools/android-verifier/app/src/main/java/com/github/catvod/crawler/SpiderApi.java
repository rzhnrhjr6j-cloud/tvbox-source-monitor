package com.github.catvod.crawler;

import android.content.pm.ActivityInfo;

import com.google.gson.JsonArray;

/**
 * Host-side placeholder for spider APIs that need the TVBox application.
 * The verifier only needs signature compatibility; unavailable app-level
 * helpers fail closed without changing spider execution.
 */
public class SpiderApi {

    public String getAddress(boolean local) {
        return "";
    }

    public String getPort() {
        return "";
    }

    public void log(String msg) {
        SpiderDebug.log(msg);
    }

    public int getScreenOrientation() {
        return ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE;
    }

    public String multiReq(JsonArray array) {
        return "";
    }

    public String webParse(String url, String flag) {
        return "";
    }
}
