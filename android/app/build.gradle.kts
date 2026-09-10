import groovy.json.JsonOutput
import groovy.json.JsonSlurper

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

val configFile = listOf("../config.json", "../config.example.json").map { rootProject.file(it) }.first { it.exists() }

@Suppress("UNCHECKED_CAST")
val calsync = JsonSlurper().parse(configFile) as Map<String, Any?>

fun javaString(value: String) = "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

android {
    namespace = "io.github.zanakis.calsync"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.zanakis.calsync"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        buildConfigField("String", "CLIENT_ID", javaString(calsync["client_id"] as String))
        buildConfigField("String", "AUTH", javaString(calsync["auth"] as? String ?: ""))
        buildConfigField("String", "TIMEZONE", javaString(calsync["timezone"] as? String ?: "UTC"))
        buildConfigField("int", "DAYS_AHEAD", ((calsync["days_ahead"] as? Number)?.toInt() ?: 60).toString())
        buildConfigField("String", "BLOCK_SUBJECT", javaString(calsync["block_subject"] as? String ?: "Busy"))
        buildConfigField(
            "String", "MIRROR_SHOW_AS",
            javaString((calsync["mirror_show_as"] as? List<*>)?.joinToString(",") ?: "busy,oof,tentative"),
        )
        buildConfigField("String", "ACCOUNTS_JSON", javaString(JsonOutput.toJson(calsync["accounts"] ?: emptyList<Any>())))
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.05.01"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    testImplementation("junit:junit:4.13.2")
}
