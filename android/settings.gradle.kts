pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
        google() // Android Gradle Plugin; potrzebne tylko tam, gdzie budowany jest :app
    }
    plugins {
        // Wersje w jednym miejscu: moduły stosują wtyczki bez wersji, więc Kotlin ładuje się raz.
        id("org.jetbrains.kotlin.jvm") version "2.2.21"
        id("org.jetbrains.kotlin.android") version "2.2.21"
        id("org.jetbrains.kotlin.plugin.compose") version "2.2.21"
        id("com.android.application") version "8.13.2"
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        mavenCentral()
        google() // androidx (tylko :app)
    }
}

rootProject.name = "iris-android"

include(":core")

// :app wymaga Android SDK (ANDROID_HOME albo local.properties). Bez niego (np. sam rdzeń na JVM) go pomijamy.
val hasAndroidSdk = listOf("ANDROID_HOME", "ANDROID_SDK_ROOT").any { !System.getenv(it).isNullOrBlank() } ||
    file("local.properties").exists()
if (hasAndroidSdk) include(":app")
