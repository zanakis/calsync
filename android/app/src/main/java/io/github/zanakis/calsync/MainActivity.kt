package io.github.zanakis.calsync

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.DateFormat
import java.util.Date

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = AccountStore(applicationContext)
        setContent { AppTheme { App(store) } }
    }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = scheme, content = content)
}

@Composable
fun App(store: AccountStore) {
    val scope = rememberCoroutineScope()
    val syncer = remember { Syncer(store) }
    var accounts by remember { mutableStateOf(store.all()) }
    var report by remember { mutableStateOf(store.lastReport) }
    var working by remember { mutableStateOf(false) }
    var askEmail by remember { mutableStateOf(false) }
    var login by remember { mutableStateOf<Auth.Pending?>(null) }
    var deviceLogin by remember { mutableStateOf<Auth.DevicePending?>(null) }
    var pollJob by remember { mutableStateOf<Job?>(null) }
    var confirmRemove by remember { mutableStateOf<Account?>(null) }

    fun launchWork(work: () -> String) {
        working = true
        scope.launch {
            report = withContext(Dispatchers.IO) { runCatching(work).getOrElse { it.message ?: it.toString() } }
            store.lastReport = report
            accounts = store.all()
            working = false
        }
    }

    fun finishAdd(result: Result<Account>) {
        deviceLogin = null
        report = result.map { store.put(it); "Added ${it.user}" }.getOrElse { it.message ?: it.toString() }
        store.lastReport = report
        accounts = store.all()
    }

    fun startDeviceLogin(email: String) {
        working = true
        scope.launch {
            val started = withContext(Dispatchers.IO) { runCatching { Auth.beginDevice(email) } }
            working = false
            started.onSuccess { pending ->
                deviceLogin = pending
                pollJob = scope.launch {
                    val result = withContext(Dispatchers.IO) { runCatching { Auth.pollDevice(pending) { isActive } } }
                    if (deviceLogin === pending) finishAdd(result)
                }
            }.onFailure { report = it.message ?: it.toString() }
        }
    }

    fun startLogin(email: String) {
        if (Auth.authMode(email) == "device") startDeviceLogin(email) else login = Auth.begin(email)
    }

    deviceLogin?.let { pending ->
        DeviceCodeScreen(pending, onCancel = { pollJob?.cancel(); deviceLogin = null })
        return
    }

    val pending = login
    if (pending != null) {
        LoginScreen(
            url = pending.url,
            onRedirect = { uri ->
                login = null
                launchWork { "Added ${Auth.finish(pending, uri).also(store::put).user}" }
            },
            onCancel = { login = null },
        )
        return
    }

    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier.safeDrawingPadding().verticalScroll(rememberScrollState()).padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text("Calendar Sync", style = MaterialTheme.typography.headlineMedium)
            if (BuildConfig.CLIENT_ID.startsWith("PASTE")) {
                Text("Set client_id in config.json and rebuild the app.", color = MaterialTheme.colorScheme.error)
            }
            Button(
                onClick = {
                    launchWork {
                        val time = DateFormat.getTimeInstance(DateFormat.SHORT).format(Date())
                        "Synced at $time\n" + syncer.sync().joinToString("\n")
                    }
                },
                enabled = !working && accounts.size >= 2,
                modifier = Modifier.fillMaxWidth().height(64.dp),
            ) {
                Text("Sync now", style = MaterialTheme.typography.titleMedium)
            }
            if (working) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (report.isNotEmpty()) Text(report, style = MaterialTheme.typography.bodyMedium)
            HorizontalDivider()
            Text("Accounts", style = MaterialTheme.typography.titleMedium)
            if (accounts.size < 2) Text("Add at least two accounts to start syncing.")
            accounts.forEach { account ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(account.user)
                        if (account.sendOnly) {
                            Text(
                                "Sends busy time only",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                    TextButton(onClick = { confirmRemove = account }, enabled = !working) { Text("Remove") }
                }
            }
            OutlinedButton(onClick = { askEmail = true }, enabled = !working, modifier = Modifier.fillMaxWidth()) {
                Text("Add account")
            }
        }
    }

    if (askEmail) {
        var email by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { askEmail = false },
            title = { Text("Add account") },
            text = {
                OutlinedTextField(
                    value = email,
                    onValueChange = { email = it },
                    label = { Text("Work email") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        askEmail = false
                        startLogin(email.trim())
                    },
                    enabled = "@" in email,
                ) { Text("Sign in") }
            },
            dismissButton = { TextButton(onClick = { askEmail = false }) { Text("Cancel") } },
        )
    }

    confirmRemove?.let { account ->
        AlertDialog(
            onDismissRequest = { confirmRemove = null },
            title = { Text("Remove ${account.user}?") },
            text = { Text("Its busy blocks are deleted from your other calendars, and the blocks this app added to its own calendar are deleted too.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmRemove = null
                    launchWork {
                        val problems = syncer.remove(account)
                        if (problems.isEmpty()) "Removed ${account.user}"
                        else "Couldn't remove ${account.user}. Try again.\n" + problems.joinToString("\n")
                    }
                }) { Text("Remove") }
            },
            dismissButton = { TextButton(onClick = { confirmRemove = null }) { Text("Cancel") } },
        )
    }
}

@Composable
fun DeviceCodeScreen(pending: Auth.DevicePending, onCancel: () -> Unit) {
    val context = LocalContext.current
    BackHandler(onBack = onCancel)
    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier.safeDrawingPadding().verticalScroll(rememberScrollState()).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("Sign in to ${pending.email}", style = MaterialTheme.typography.headlineSmall)
            Text("On any device, open the page below and enter this code:", style = MaterialTheme.typography.bodyLarge)
            Text(pending.userCode, style = MaterialTheme.typography.displaySmall)
            Button(
                onClick = {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("code", pending.userCode))
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(pending.verificationUri)))
                },
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Copy code and open sign-in page")
            }
            Text(pending.verificationUri, style = MaterialTheme.typography.bodyMedium)
            LinearProgressIndicator(Modifier.fillMaxWidth())
            Text("Waiting for you to finish signing in…", style = MaterialTheme.typography.bodyMedium)
            TextButton(onClick = onCancel) { Text("Cancel") }
        }
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun LoginScreen(url: String, onRedirect: (Uri) -> Unit, onCancel: () -> Unit) {
    val redirect by rememberUpdatedState(onRedirect)
    BackHandler(onBack = onCancel)
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.safeDrawingPadding()) {
            TextButton(onClick = onCancel, modifier = Modifier.padding(8.dp)) { Text("Cancel") }
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context ->
                    WebView(context).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        webViewClient = object : WebViewClient() {
                            private var done = false

                            private fun intercept(uri: Uri): Boolean {
                                if (uri.scheme != "http" || uri.host != "localhost") return false
                                if (!done) {
                                    done = true
                                    redirect(uri)
                                }
                                return true
                            }

                            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) =
                                intercept(request.url)

                            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                                if (intercept(Uri.parse(url))) view.stopLoading()
                            }
                        }
                        loadUrl(url)
                    }
                },
            )
        }
    }
}
