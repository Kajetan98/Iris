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
import androidx.compose.material3.RadioButton
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
import pl.iris.core.EngineKind
import pl.iris.app.UpdateState
import pl.iris.core.Settings
import pl.iris.core.UpdateInfo

@Composable
fun SettingsScreen(
    initial: Settings,
    importStatus: String?,
    onPickModel: () -> Unit,
    onRemoveModel: () -> Unit,
    versionName: String,
    update: UpdateState,
    onCheckUpdate: () -> Unit,
    onInstallUpdate: (UpdateInfo) -> Unit,
    wakeInstalled: Boolean,
    wakeStatus: String?,
    onDownloadWake: () -> Unit,
    onPickWakeZip: () -> Unit,
    onRemoveWake: () -> Unit,
    onSave: (Settings) -> Unit,
    onBack: () -> Unit,
) {
    var engine by remember(initial.engine) { mutableStateOf(initial.engine) }
    var key by remember(initial) { mutableStateOf(initial.apiKey) }
    var workspace by remember(initial) { mutableStateOf(initial.workspaceId) }
    var model by remember(initial) { mutableStateOf(initial.model) }
    var useMemory by remember(initial) { mutableStateOf(initial.useMemory) }
    var learn by remember(initial) { mutableStateOf(initial.learn) }
    var offline by remember(initial) { mutableStateOf(initial.preferOffline) }
    var checkUpdates by remember(initial) { mutableStateOf(initial.checkUpdates) }
    var wake by remember(initial) { mutableStateOf(initial.wakeWord) }

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

        Text("Silnik rozmowy", color = IrisColors.Ink, fontFamily = FontFamily.Serif, fontSize = 18.sp)
        EngineRow("Claude API", "Model w chmurze Anthropic. Wymaga klucza API i jest płatny według zużycia.", engine == EngineKind.CLAUDE) { engine = EngineKind.CLAUDE }
        EngineRow("Model na telefonie", "Działa bez konta, klucza i opłat. Rozmowa i pamięć zostają na telefonie. Odpowiada wolniej i gorzej niż Claude.", engine == EngineKind.LOCAL) { engine = EngineKind.LOCAL }

        if (engine == EngineKind.CLAUDE) {
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

        } else {
            Text(
                if (initial.localModel.isBlank()) "Nie wybrano pliku modelu." else "Model: ${initial.localModel}",
                color = IrisColors.Ink,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = onPickModel) { Text(if (initial.localModel.isBlank()) "Wybierz plik modelu" else "Zmień model") }
                if (initial.localModel.isNotBlank()) OutlinedButton(onClick = onRemoveModel) { Text("Usuń model") }
            }
            if (importStatus != null) Text(importStatus, color = IrisColors.Ink)
            Text(
                "Potrzebny jest plik .litertlm (format LiteRT-LM). Pobierz go na telefon z Hugging Face, z organizacji litert-community, " +
                    "np. Qwen3-0.6B (mały) albo gemma-4-E2B-it-litert-lm (ok. 2,6 GB, lepszy). Niektóre modele wymagają zalogowania i " +
                    "akceptacji licencji. Aplikacja kopiuje plik do swojej pamięci, więc na czas kopiowania potrzeba tyle wolnego miejsca, ile zajmuje model. " +
                    "Pierwsza odpowiedź po wyborze modelu trwa dłużej, bo model się wczytuje.",
                color = IrisColors.Ink2,
                fontSize = 14.sp,
            )
        }

        SwitchRow(
            "Używaj pamięci w rozmowie",
            if (engine == EngineKind.LOCAL) "Fakty o tobie trafiają do kontekstu modelu na telefonie i nigdzie nie są wysyłane. Wyłączone: model ich nie widzi."
            else "Fakty o tobie trafiają do kontekstu modelu, czyli są wysyłane do Anthropic. Wyłączone: model ich nie widzi.",
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

        SwitchRow(
            "Hasło wybudzające",
            "Powiedz „Hej Iris” albo „Irysku”, zamiast dotykać Mów. Działa tylko przy otwartej aplikacji (ekran nie wygasa), " +
                "rozpoznawanie jest offline na telefonie i nic nie jest wysyłane. Zużywa baterię i czasem się myli.",
            wake,
        ) { wake = it }
        if (wake) {
            Text(if (wakeInstalled) "Model hasła: zainstalowany." else "Model hasła: brak. Pobierz go (ok. 50 MB), żeby hasło działało.", color = IrisColors.Ink)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                if (!wakeInstalled) {
                    Button(onClick = onDownloadWake) { Text("Pobierz model") }
                    OutlinedButton(onClick = onPickWakeZip) { Text("Wybierz plik .zip") }
                } else {
                    OutlinedButton(onClick = onRemoveWake) { Text("Usuń model hasła") }
                }
            }
            if (wakeStatus != null) Text(wakeStatus, color = IrisColors.Ink2, fontSize = 14.sp)
        }

        SwitchRow(
            "Sprawdzaj aktualizacje przy starcie",
            "Przy uruchomieniu aplikacja pyta GitHuba o nowszą wersję. Wysyła tylko zwykłe żądanie HTTPS (adres IP), bez rozmów, faktów i klucza.",
            checkUpdates,
        ) { checkUpdates = it }

        Text("Wersja $versionName", color = IrisColors.Ink)
        val status = when (update) {
            UpdateState.Idle -> null
            UpdateState.Checking -> "Sprawdzam..."
            UpdateState.UpToDate -> "Masz najnowszą wersję."
            is UpdateState.Available -> "Dostępna wersja ${update.info.versionName}."
            is UpdateState.Downloading -> "Pobieram... ${update.percent}%"
            is UpdateState.NeedsPermission -> "Zezwól IRIS na instalowanie aplikacji w ustawieniach systemu, potem dotknij Zainstaluj."
            is UpdateState.Failed -> update.reason
        }
        if (status != null) Text(status, color = IrisColors.Ink2, fontSize = 14.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedButton(onClick = onCheckUpdate) { Text("Sprawdź aktualizacje") }
            val offered = (update as? UpdateState.Available)?.info ?: (update as? UpdateState.NeedsPermission)?.info ?: (update as? UpdateState.Failed)?.info
            if (offered != null) Button(onClick = { onInstallUpdate(offered) }) { Text("Zainstaluj") }
        }

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = {
                onSave(Settings(engine = engine, localModel = initial.localModel, apiKey = key.trim(), workspaceId = workspace.trim(), model = model.trim().ifEmpty { DEFAULT_CLAUDE_MODEL }, useMemory = useMemory, learn = learn, preferOffline = offline, checkUpdates = checkUpdates, wakeWord = wake))
                onBack()
            }) { Text("Zapisz") }
            OutlinedButton(onClick = onBack) { Text("Anuluj") }
        }
    }
}

@Composable
private fun EngineRow(title: String, description: String, selected: Boolean, onSelect: () -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        RadioButton(selected = selected, onClick = onSelect)
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, color = IrisColors.Ink)
            Text(description, color = IrisColors.Ink2, fontSize = 14.sp)
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
