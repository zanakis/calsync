package io.github.zanakis.calsync

import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

class AuthException(message: String) : Exception(message)

object Auth {
    const val REDIRECT_URI = "http://localhost"
    private const val SCOPES = "https://graph.microsoft.com/Calendars.ReadWrite offline_access openid profile"

    class Pending(
        val url: String,
        val state: String,
        val verifier: String,
        val clientId: String,
        val tenant: String,
        val email: String,
    )

    class DevicePending(
        val deviceCode: String,
        val userCode: String,
        val verificationUri: String,
        val interval: Long,
        val expiresAt: Long,
        val clientId: String,
        val tenant: String,
        val email: String,
    )

    private fun base(tenant: String) = "https://login.microsoftonline.com/$tenant/oauth2/v2.0"

    fun configFor(vararg emails: String): JSONObject? {
        val accounts = JSONArray(BuildConfig.ACCOUNTS_JSON)
        return (0 until accounts.length()).map { accounts.getJSONObject(it) }
            .firstOrNull { entry -> emails.any { it.isNotBlank() && entry.optString("email").equals(it, ignoreCase = true) } }
    }

    private fun clientId(entry: JSONObject?) = entry?.optString("client_id").orEmpty().ifEmpty { BuildConfig.CLIENT_ID }

    private fun tenant(entry: JSONObject?) = entry?.optString("tenant").orEmpty().ifEmpty { "common" }

    // First-party workaround client IDs have no localhost redirect, so those accounts must use the device flow.
    // The default follows that: device when the account overrides client_id, browser otherwise; "auth" overrides.
    fun authMode(email: String): String {
        val entry = configFor(email)
        entry?.optString("auth")?.lowercase()?.takeIf { it.isNotEmpty() }?.let { return it }
        BuildConfig.AUTH.lowercase().takeIf { it.isNotEmpty() }?.let { return it }
        return if (entry?.optString("client_id").orEmpty().isNotEmpty()) "device" else "browser"
    }

    fun begin(email: String): Pending {
        val match = configFor(email)
        val clientId = clientId(match)
        val tenant = tenant(match)
        val verifier = randomString(48)
        val challenge = base64Url(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))
        val state = randomString(16)
        val url = Uri.parse("${base(tenant)}/authorize").buildUpon()
            .appendQueryParameter("client_id", clientId)
            .appendQueryParameter("response_type", "code")
            .appendQueryParameter("redirect_uri", REDIRECT_URI)
            .appendQueryParameter("scope", SCOPES)
            .appendQueryParameter("code_challenge", challenge)
            .appendQueryParameter("code_challenge_method", "S256")
            .appendQueryParameter("state", state)
            .appendQueryParameter("prompt", "select_account")
            .appendQueryParameter("login_hint", email)
            .build()
            .toString()
        return Pending(url, state, verifier, clientId, tenant, email)
    }

    fun finish(pending: Pending, redirect: Uri): Account {
        redirect.getQueryParameter("error")?.let {
            throw AuthException(redirect.getQueryParameter("error_description") ?: it)
        }
        val code = redirect.getQueryParameter("code")
        if (code == null || redirect.getQueryParameter("state") != pending.state) {
            throw AuthException("No valid sign-in response")
        }
        val t = tokenRequest(
            pending.tenant,
            mapOf(
                "grant_type" to "authorization_code",
                "client_id" to pending.clientId,
                "code" to code,
                "redirect_uri" to REDIRECT_URI,
                "code_verifier" to pending.verifier,
                "scope" to SCOPES,
            ),
        )
        return accountFromToken(t, pending.email, pending.clientId, pending.tenant)
    }

    fun beginDevice(email: String): DevicePending {
        val match = configFor(email)
        val clientId = clientId(match)
        val tenant = tenant(match)
        val json = tokenRequestRaw(tenant, "devicecode", mapOf("client_id" to clientId, "scope" to SCOPES))
        if (!json.has("device_code")) throw AuthException(errorText(json))
        return DevicePending(
            deviceCode = json.getString("device_code"),
            userCode = json.getString("user_code"),
            verificationUri = json.optString("verification_uri").ifEmpty { json.optString("verification_url") },
            interval = json.optLong("interval", 5),
            expiresAt = System.currentTimeMillis() + json.optLong("expires_in", 900) * 1000,
            clientId = clientId,
            tenant = tenant,
            email = email,
        )
    }

    fun pollDevice(pending: DevicePending, active: () -> Boolean): Account {
        var interval = pending.interval
        while (active() && System.currentTimeMillis() < pending.expiresAt) {
            var waited = 0L
            while (active() && waited < interval * 1000) {
                Thread.sleep(500)
                waited += 500
            }
            if (!active()) throw AuthException("Cancelled")
            val t = tokenRequestRaw(
                pending.tenant, "token",
                mapOf(
                    "grant_type" to "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id" to pending.clientId,
                    "device_code" to pending.deviceCode,
                ),
            )
            when (t.optString("error")) {
                "authorization_pending" -> continue
                "slow_down" -> interval += 5
                "" -> return accountFromToken(t, pending.email, pending.clientId, pending.tenant)
                else -> throw AuthException(errorText(t))
            }
        }
        throw AuthException("Sign-in timed out")
    }

    fun refresh(account: Account): Account {
        val t = tokenRequest(
            account.tenant,
            mapOf(
                "grant_type" to "refresh_token",
                "client_id" to account.clientId,
                "refresh_token" to account.refreshToken,
                "scope" to SCOPES,
            ),
        )
        return account.copy(
            refreshToken = t.optString("refresh_token").ifEmpty { account.refreshToken },
            accessToken = t.getString("access_token"),
            expiresAt = expiry(t),
        )
    }

    private fun accountFromToken(t: JSONObject, email: String, clientId: String, tenant: String): Account {
        val claims = JSONObject(String(Base64.getUrlDecoder().decode(t.getString("id_token").split(".")[1])))
        return Account(
            oid = claims.getString("oid"),
            email = email,
            user = claims.optString("preferred_username").ifEmpty { claims.optString("upn") },
            clientId = clientId,
            tenant = tenant,
            refreshToken = t.getString("refresh_token"),
            accessToken = t.getString("access_token"),
            expiresAt = expiry(t),
        )
    }

    private fun tokenRequest(tenant: String, form: Map<String, String>): JSONObject {
        val json = tokenRequestRaw(tenant, "token", form)
        if (!json.has("access_token")) throw AuthException(errorText(json))
        return json
    }

    private fun tokenRequestRaw(tenant: String, endpoint: String, form: Map<String, String>): JSONObject {
        val body = form.entries.joinToString("&") { "${enc(it.key)}=${enc(it.value)}" }
        val response = Http.send("POST", "${base(tenant)}/$endpoint", mapOf("Content-Type" to "application/x-www-form-urlencoded"), body)
        return runCatching { JSONObject(response.text) }.getOrElse { throw AuthException("Sign-in failed (${response.code})") }
    }

    private fun errorText(json: JSONObject) =
        json.optString("error_description").ifEmpty { json.optString("error", "Sign-in failed") }.lineSequence().first()

    private fun expiry(t: JSONObject) = System.currentTimeMillis() + (t.optLong("expires_in", 3600) - 300) * 1000

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

    private fun randomString(bytes: Int) = base64Url(ByteArray(bytes).also { SecureRandom().nextBytes(it) })

    private fun base64Url(bytes: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
}
