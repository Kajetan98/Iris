import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "pl.iris.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "pl.iris.app"
        minSdk = 26
        targetSdk = 36
        // CI podaje numer uruchomienia workflow (-PversionCode=N), więc każdy build jest nowszy od poprzedniego;
        // aplikacja porównuje go z latest.json w wydaniu (spec, sekcja 14, "Aktualizacje").
        val code = (project.findProperty("versionCode") as String?)?.toIntOrNull() ?: 1
        versionCode = code
        versionName = "0.1.$code"

        // LiteRT-LM ma biblioteki natywne tylko dla 64-bitowych układów; 32-bitowe telefony nie uruchomią modelu lokalnego.
        ndk { abiFilters += "arm64-v8a" }
    }

    // Klucz debug w repozytorium: każdy build z CI ma ten sam podpis, więc nową wersję można
    // zainstalować na starą bez odinstalowywania (i bez utraty faktów). To nie jest klucz wydawniczy.
    signingConfigs {
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
        allWarningsAsErrors.set(false)
    }
}

dependencies {
    implementation(project(":core"))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.google.ai.edge.litertlm:litertlm-android:0.18.0")
    implementation("com.alphacephei:vosk-android:0.3.75") // hasło wybudzające, offline

    implementation(platform("androidx.compose:compose-bom:2025.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
}
