package io.github.zanakis.calsync

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

data class Account(
    val oid: String,
    val email: String,
    val user: String,
    val clientId: String,
    val tenant: String,
    val refreshToken: String,
    val accessToken: String,
    val expiresAt: Long,
) {
    val sendOnly: Boolean
        get() = Auth.configFor(email, user)?.optBoolean("receive_blocks", true) == false

    fun toJson(): JSONObject = JSONObject()
        .put("oid", oid)
        .put("email", email)
        .put("user", user)
        .put("clientId", clientId)
        .put("tenant", tenant)
        .put("refreshToken", refreshToken)
        .put("accessToken", accessToken)
        .put("expiresAt", expiresAt)

    companion object {
        fun fromJson(o: JSONObject) = Account(
            oid = o.getString("oid"),
            email = o.optString("email").ifEmpty { o.getString("user") },
            user = o.getString("user"),
            clientId = o.getString("clientId"),
            tenant = o.getString("tenant"),
            refreshToken = o.getString("refreshToken"),
            accessToken = o.getString("accessToken"),
            expiresAt = o.getLong("expiresAt"),
        )
    }
}

class AccountStore(context: Context) {
    private val prefs = context.getSharedPreferences("calsync", Context.MODE_PRIVATE)

    @Synchronized
    fun all(): List<Account> {
        val array = JSONArray(prefs.getString("accounts", "[]"))
        return (0 until array.length()).map { Account.fromJson(array.getJSONObject(it)) }
    }

    @Synchronized
    fun put(account: Account) {
        val current = all()
        save(
            if (current.any { it.oid == account.oid }) current.map { if (it.oid == account.oid) account else it }
            else current + account
        )
    }

    @Synchronized
    fun remove(oid: String) = save(all().filter { it.oid != oid })

    var lastReport: String
        get() = prefs.getString("report", "") ?: ""
        set(value) = prefs.edit().putString("report", value).apply()

    private fun save(accounts: List<Account>) {
        prefs.edit().putString("accounts", JSONArray(accounts.map { it.toJson() }).toString()).commit()
    }
}
