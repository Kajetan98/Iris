package pl.iris.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import pl.iris.core.DEFAULT_CLAUDE_MODEL
import pl.iris.core.Settings

@Composable
fun SettingsScreen(initial: Settings, onSave: (Settings) -> Unit, onBack: () -> Unit) {
    var key by remember(initial) { mutableStateOf(initial.apiKey) }
    var workspace by remember(initial) { mutableStateOf(initial.workspaceId) }
    var model by remember(initial) { mutableStateOf(initial.model) }
    var useMemory by remember(initial) { mutableStateOf(initial.useMemory) }
    var learn by remember(initial) { mutableStateOf(initial.learn) }
    var offline by remember(initial) { mutableStateOf(initial.preferOffline) }

    Column(
        Modifier
            .fillMaxSize()
            .background(IrisColors.Bg)
            .safeDrawingPadding()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Ustawienia", color = IrisColors.Ink, fontFamily = FontFamily.Serif, fontSize = 24.sp)

        OutlinedTextField(
            value = key,
            onValueChange = { key = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Klucz API Claude") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        )
        Text(
            "Klucz jest zapisany tylko na tym telefonie, zaszyfrowany kluczem z Android Keystore, i nie trafia do kopii zapasowej. " +
                "Wysyłany jest wyłącznie do Anthropic przy rozmowie. Użyj klucza z limitem wydatków.",
            color = IrisColors.Ink2,
            fontSize = 14.sp,
        )

        OutlinedTextField(
            value = workspace,
            onValueChange = { workspace = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("ID workspace (opcjonalnie)") },
            singleLine = true,
            supportingText = { Text("Puste, jeśli klucz jest przypisany do workspace. Inaczej wpisz ID w postaci wrkspc_...") },
        )

        OutlinedTextField(
            value = model,
            onValueChange = { model = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Model") },
            singleLine = true,
            supportingText = { Text("Puste: $DEFAULT_CLAUDE_MODEL") },
        )

        SwitchRow(
            "Używaj pamięci w rozmowie",
            "Fakty o tobie trafiają do kontekstu modelu, czyli są wysyłane do Anthropic. Wyłączone: model ich nie widzi.",
            useMemory,
        ) { useMemory = it }
        SwitchRow(
            "Ucz się z rozmów",
            "Po każdej odpowiedzi osobne zapytanie do modelu wyciąga fakty z twojej wypowiedzi (tylko z niej, bez historii). " +
                "Przypuszczenia są słabe, dopóki się nie powtórzą, a IRIS pyta o nie. Wyłączone: zapisuje tylko to, co powiesz poleceniem „zapamiętaj”.",
            learn,
        ) { learn = it }
        SwitchRow(
            "Preferuj rozpoznawanie offline",
            "Prosi system o rozpoznawanie mowy bez wysyłania nagrania do usługi mowy. Działa tylko z zainstalowanym pakietem języka polskiego i bez gwarancji.",
            offline,
        ) { offline = it }

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = {
                onSave(Settings(apiKey = key.trim(), workspaceId = workspace.trim(), model = model.trim().ifEmpty { DEFAULT_CLAUDE_MODEL }, useMemory = useMemory, learn = learn, preferOffline = offline))
                onBack()
            }) { Text("Zapisz") }
            OutlinedButton(onClick = onBack) { Text("Anuluj") }
        }
    }
}

@Composable
private fun SwitchRow(title: String, description: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, color = IrisColors.Ink)
            Text(description, color = IrisColors.Ink2, fontSize = 14.sp)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
