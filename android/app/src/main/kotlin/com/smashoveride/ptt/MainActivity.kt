package com.smashoveride.ptt

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Bundle
import android.os.Message
import android.view.KeyEvent
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.core.content.ContextCompat

class MainActivity : ComponentActivity() {

    private lateinit var webView: WebView
    private var serverStarted = false

    // PythonServerService starts the Flask server asynchronously (Python
    // startup + Flask app.run() aren't instant) and broadcasts the bound
    // port once it's ready, since MainActivity doesn't call into Chaquopy
    // directly -- the service owns the Python process's lifetime.
    private val portReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val port = intent.getIntExtra(PythonServerService.EXTRA_PORT, -1)
            if (port > 0) {
                loadServer(port)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        configureWebView(webView)

        val filter = IntentFilter(PythonServerService.ACTION_SERVER_READY)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(portReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(portReceiver, filter)
        }

        ContextCompat.startForegroundService(this, Intent(this, PythonServerService::class.java))

        // Covers the case where the service (and Flask) were already
        // running before this Activity was (re)created -- e.g. rotation,
        // or reopening the app after backgrounding it -- since no fresh
        // broadcast will be sent in that case.
        val existingPort = PythonServerService.lastKnownPort
        if (existingPort > 0) {
            loadServer(existingPort)
        }
    }

    private fun loadServer(port: Int) {
        if (serverStarted) return
        serverStarted = true
        webView.loadUrl("http://127.0.0.1:$port/#/recent")
    }

    private fun configureWebView(webView: WebView) {
        webView.settings.apply {
            javaScriptEnabled = true
            // Required for the frontend's theme + per-table column-order
            // persistence (frontend/app.js), which uses localStorage.
            domStorageEnabled = true
            // Lets the frontend detect it's running inside this app (see
            // frontend/app.js's PoliticianTradesAndroid check) to hide the
            // Shut Down/Restart Server controls, which don't make sense
            // when the "server" is this app's own process.
            userAgentString = "$userAgentString PoliticianTradesAndroid/1.0"
            javaScriptCanOpenWindowsAutomatically = true
            setSupportMultipleWindows(true)
        }

        webView.webViewClient = WebViewClient()
        webView.webChromeClient = object : WebChromeClient() {
            override fun onCreateWindow(
                view: WebView,
                isDialog: Boolean,
                isUserGesture: Boolean,
                resultMsg: Message,
            ): Boolean {
                // The app's three window.open() call sites (Yahoo Finance
                // news link, GitHub release page, official filing PDF link
                // -- see frontend/app.js) are always genuinely external
                // destinations. A plain WebView doesn't implement
                // window.open() at all by default, so without this
                // override those links would silently do nothing; this
                // captures the target URL via a throwaway WebView and hands
                // it to the system browser instead of trying to display it
                // in a second in-app WebView.
                val transport = resultMsg.obj as WebView.WebViewTransport
                val throwaway = WebView(view.context)
                throwaway.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(
                        v: WebView,
                        request: WebResourceRequest,
                    ): Boolean {
                        startActivity(Intent(Intent.ACTION_VIEW, request.url))
                        return true
                    }
                }
                transport.webView = throwaway
                resultMsg.sendToTarget()
                return true
            }
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            // The frontend's router is hash-based (location.hash, see
            // frontend/app.js) -- each in-app navigation is a real history
            // entry, so stepping back through WebView history is exactly
            // the app's own "back" behavior, not a page reload.
            webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onDestroy() {
        super.onDestroy()
        unregisterReceiver(portReceiver)
    }
}
