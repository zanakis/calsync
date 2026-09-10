package io.github.zanakis.calsync

import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

const val BLOCK_TAG = "String {6f1c2a52-8e4b-4d8e-9a57-3c0f5b7d2e91} Name CalSyncSource"
const val CREATION_TIME = "SystemTime 0x3007"

class Response(val code: Int, val text: String, val retryAfter: String?)

object Http {
    fun send(method: String, url: String, headers: Map<String, String>, body: String? = null): Response {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = 30_000
            conn.readTimeout = 60_000
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            val stream = if (code < 400) conn.inputStream else conn.errorStream
            return Response(code, stream?.bufferedReader()?.use { it.readText() } ?: "", conn.getHeaderField("Retry-After"))
        } finally {
            conn.disconnect()
        }
    }
}

class GraphException(message: String) : Exception(message)

class Graph(private val token: String) {
    private val retryable = setOf(429, 500, 502, 503, 504)

    private fun call(method: String, path: String, body: JSONObject? = null): JSONObject? {
        val url = if (path.startsWith("https://")) path else "https://graph.microsoft.com/v1.0$path"
        val headers = buildMap {
            put("Authorization", "Bearer $token")
            put("Prefer", "outlook.timezone=\"${BuildConfig.TIMEZONE}\"")
            if (body != null) put("Content-Type", "application/json")
        }
        for (attempt in 0 until 5) {
            val last = attempt == 4
            val r = try {
                Http.send(method, url, headers, body?.toString())
            } catch (e: IOException) {
                if (last) throw GraphException("Network error: ${e.message}")
                Thread.sleep(1000L shl attempt)
                continue
            }
            if (r.code in retryable && !last) {
                Thread.sleep(r.retryAfter?.toDoubleOrNull()?.coerceAtMost(60.0)?.let { (it * 1000).toLong() } ?: (1000L shl attempt))
                continue
            }
            if (r.code < 300) return r.text.takeIf { it.isNotBlank() }?.let(::JSONObject)
            if (method == "DELETE" && r.code == 404) return null
            val message = runCatching { JSONObject(r.text).getJSONObject("error").getString("message") }.getOrElse { r.text.take(200) }
            throw GraphException("$method ${url.substringBefore('?')} failed (${r.code}): $message")
        }
        throw IllegalStateException()
    }

    fun events(start: String, end: String): List<Event> {
        val query = linkedMapOf(
            "startDateTime" to start,
            "endDateTime" to end,
            // Graph silently drops the expanded properties unless $select includes organizer (or there is no $select).
            "\$select" to "start,end,showAs,isAllDay,isCancelled,subject,iCalUId,organizer",
            "\$expand" to "singleValueExtendedProperties(\$filter=id eq '$BLOCK_TAG' or id eq '$CREATION_TIME')",
            "\$top" to "250",
        )
        var next: String? = "/me/calendarView?" + query.entries.joinToString("&") { "${it.key}=${Uri.encode(it.value)}" }
        val events = mutableListOf<Event>()
        while (next != null) {
            val page = call("GET", next)!!
            val values = page.getJSONArray("value")
            for (i in 0 until values.length()) events += parse(values.getJSONObject(i))
            next = page.optString("@odata.nextLink").ifEmpty { null }
        }
        return events
    }

    private fun parse(o: JSONObject): Event {
        val props = o.optJSONArray("singleValueExtendedProperties")
        // Every event has a creation time, so a missing one means the expansion was dropped and tags can't be trusted.
        if (props == null || props.length() == 0) {
            throw GraphException("Graph didn't return block tags, so this calendar was left alone")
        }
        val tag = (0 until props.length()).map { props.getJSONObject(it) }
            .firstOrNull { it.optString("id").equals(BLOCK_TAG, ignoreCase = true) }?.optString("value")
        return Event(
            id = o.getString("id"),
            tag = tag,
            start = o.getJSONObject("start").getString("dateTime").take(19),
            end = o.getJSONObject("end").getString("dateTime").take(19),
            showAs = o.optString("showAs"),
            isAllDay = o.optBoolean("isAllDay"),
            isCancelled = o.optBoolean("isCancelled"),
            iCalUId = o.text("iCalUId"),
            subject = o.text("subject"),
        )
    }

    private fun JSONObject.text(name: String) = if (isNull(name)) null else optString(name).ifEmpty { null }

    fun delete(id: String) {
        call("DELETE", "/me/events/${Uri.encode(id)}")
    }

    fun createBlock(key: BlockKey) {
        val tz = BuildConfig.TIMEZONE
        call(
            "POST", "/me/events",
            JSONObject()
                .put("subject", BuildConfig.BLOCK_SUBJECT)
                .put("start", JSONObject().put("dateTime", key.start).put("timeZone", tz))
                .put("end", JSONObject().put("dateTime", key.end).put("timeZone", tz))
                .put("isAllDay", key.allDay)
                .put("showAs", key.showAs)
                .put("sensitivity", "private")
                .put("isReminderOn", false)
                .put("responseRequested", false)
                .put("singleValueExtendedProperties", JSONArray().put(JSONObject().put("id", BLOCK_TAG).put("value", key.source))),
        )
    }
}
