// Top-level build file. Plugin versions are declared here (with
// `apply false`) and actually applied per-module in app/build.gradle.kts --
// standard Gradle convention, keeps a single place to bump versions.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("com.chaquo.python") version "17.0.0" apply false
}
