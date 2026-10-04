package com.github.catvod.crawler;

public class SpiderDebug {

    public static void log(Throwable error) {
        try {
            android.util.Log.d("SpiderLog", error.getMessage(), error);
        } catch (Throwable ignored) {
        }
    }

    public static void log(String message) {
        try {
            android.util.Log.d("SpiderLog", message);
        } catch (Throwable ignored) {
        }
    }

    public static String ec(int code) {
        return "";
    }
}
