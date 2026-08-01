plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// Single source of truth for the version string lives in backend/version.py
// (shared with the desktop PyInstaller builds) -- read it here instead of
// duplicating it, so a release bump only ever happens in one place.
val backendVersionFile = rootProject.file("../backend/version.py")
val appVersionName = Regex("""APP_VERSION\s*=\s*"([^"]+)"""")
    .find(backendVersionFile.readText())
    ?.groupValues
    ?.get(1)
    ?: "0.0.0"
val versionParts = appVersionName.split(".").map { it.toIntOrNull() ?: 0 }
val appVersionCode = versionParts.getOrElse(0) { 0 } * 10000 +
    versionParts.getOrElse(1) { 0 } * 100 +
    versionParts.getOrElse(2) { 0 }

android {
    namespace = "com.smashoveride.ptt"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.smashoveride.ptt.lite"
        minSdk = 24
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersionName

        ndk {
            // arm64-v8a covers virtually all real phones from ~2017 on;
            // x86_64 is included for emulator testing, per Chaquopy's own
            // recommendation for typical projects.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    signingConfigs {
        create("release") {
            // Populated by the CI workflow (build-android.yml) from GitHub
            // Actions secrets -- see README's "Android build & signing"
            // section for the one-time local keystore-generation step.
            // Falling back to a relative filename (rather than null) keeps
            // Gradle configuration itself from failing when these aren't
            // set (e.g. a plain `assembleDebug` run) -- it only matters
            // once `assembleRelease` actually tries to sign.
            storeFile = file(System.getenv("ANDROID_KEYSTORE_PATH") ?: "release.keystore")
            storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
            keyAlias = System.getenv("ANDROID_KEY_ALIAS")
            keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = false
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            // Mirrors the "Lite" desktop build's runtime deps (see
            // requirements.txt + packaging/*_lite.spec's excludes) -- no
            // pdfplumber/lxml/pytesseract/PIL/pypdfium2, since those pull
            // in native code Chaquopy would need prebuilt Android wheels
            // for. Lite already avoids the live PDF/OCR pipeline entirely
            // in favor of backend/snapshot_download.py, so none of that is
            // needed here either.
            install("Flask>=3.0,<4.0")
            install("flask-cors>=4.0,<5.0")
            install("requests>=2.31,<3.0")
            install("urllib3>=1.26,<3.0")
            install("PyYAML>=6.0,<7.0")
        }
    }
}

// Chaquopy needs backend/ and frontend/ to land as sibling directories
// under one Python source root (app.py's FRONTEND_DIR is resolved relative
// to backend/'s parent) -- pointing Chaquopy's srcDirs directly at the
// existing top-level backend/frontend folders would instead flatten their
// *contents* into one root, breaking that. Staging a filtered copy here
// keeps backend/ and frontend/ as the single source of truth for both the
// desktop and Android builds, without checking a duplicate into git (see
// .gitignore).
val pySrcDir = "$projectDir/src/main/python"

// Mirrors packaging/macos_lite.spec's `excludes` list: the live
// PDF/OCR pipeline and its dependents aren't part of the Lite data path
// (backend/data_fetch.py's pipeline_available() already tolerates these
// imports failing and falls back to snapshot_download.py).
val excludedBackendPaths = listOf(
    "pipeline/orchestrator.py",
    "pipeline/house_clerk.py",
    "pipeline/senate_efd.py",
    "pipeline/secondary_sources.py",
    "pipeline/custom_api_source.py",
    "pipeline/checkbox_form.py",
    "pipeline/ocr.py",
    "ticker_resolve.py",
    "__pycache__/**",
    "**/__pycache__/**",
    "**/*.pyc",
)

tasks.register<Delete>("cleanPythonSources") {
    delete(pySrcDir)
}

tasks.register<Copy>("syncPythonSources") {
    dependsOn("cleanPythonSources")
    into(pySrcDir)
    from(rootProject.file("../backend")) {
        into("backend")
        exclude(excludedBackendPaths)
    }
    from(rootProject.file("../frontend")) {
        into("frontend")
    }
}

tasks.named("preBuild") {
    dependsOn("syncPythonSources")
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
