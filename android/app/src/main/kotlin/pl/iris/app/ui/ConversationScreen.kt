package pl.iris.app.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import pl.iris.app.R
import pl.iris.core.Phase
import pl.iris.core.Texts
import pl.iris.core.Turn
import pl.iris.core.UiState

@Composable
fun ConversationScreen(
    ui: UiState,
    onTalk: () -> Unit,
    onSubmit: (String) -> Unit,
    onDismissNotice: () -> Unit,
    onOpenMemory: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    var text by rememberSaveable { mutableStateOf("") }
    val listState = rememberLazyListState()
    LaunchedEffect(ui.turns.size, ui.turns.lastOrNull()?.reply) {
        if (ui.turns.isNotEmpty()) listState.animateScrollToItem(ui.turns.lastIndex)
    }

    Column(
        Modifier
            .fillMaxSize()
            .background(IrisColors.Bg)
            .safeDrawingPadding(),
    ) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Image(
                painter = painterResource(R.drawable.iris_wordmark),
                contentDescription = "IRIS",
                modifier = Modifier.height(20.dp).aspectRatio(616f / 177f),
                colorFilter = ColorFilter.tint(IrisColors.Ink),
            )
            Box(Modifier.weight(1f))
            TextButton(onClick = onOpenMemory) { Text("Pamięć", color = IrisColors.Ink2) }
            TextButton(onClick = onOpenSettings) { Text("Ustawienia", color = IrisColors.Ink2) }
        }

        WaveCanvas(ui.phase, ui.level, Modifier.fillMaxWidth().height(180.dp))
        Text(
            Texts.state(ui.phase, ui.ready),
            color = IrisColors.Ink2,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp).semantics { contentDescription = "Stan: ${Texts.state(ui.phase, ui.ready)}" },
        )

        if (ui.turns.isEmpty()) {
            Text(
                "Tu pojawi się zapis rozmowy. Zostaje tylko w pamięci aplikacji.",
                color = IrisColors.Ink2,
                textAlign = TextAlign.Center,
                modifier = Modifier.weight(1f).fillMaxWidth().padding(20.dp),
            )
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 20.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(20.dp),
            ) {
                items(ui.turns, key = { it.id }) { turn -> TurnView(turn, current = turn.id == ui.turns.last().id) }
            }
        }

        ui.notice?.let { notice ->
            Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(notice, color = IrisColors.Warn, modifier = Modifier.weight(1f))
                TextButton(onClick = onDismissNotice) { Text("OK", color = IrisColors.Ink2) }
            }
        }

        Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            TalkButton(ui.phase, onTalk)
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Albo napisz tutaj", color = IrisColors.Ink2) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = {
                    if (text.isNotBlank()) {
                        onSubmit(text)
                        text = ""
                    }
                }),
            )
        }
    }
}

@Composable
private fun TurnView(turn: Turn, current: Boolean) {
    Column(Modifier.fillMaxWidth().alpha(if (current) 1f else 0.6f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        turn.heard?.let { heard ->
            Text(
                buildAnnotatedString {
                    withStyle(SpanStyle(color = IrisColors.You, fontWeight = FontWeight.SemiBold)) { append("Ty") }
                    append(" $heard")
                },
                color = IrisColors.Ink2,
            )
        }
        turn.reply?.let { reply ->
            Text(
                reply,
                color = IrisColors.Ink,
                fontFamily = FontFamily.Serif,
                fontSize = if (current) 22.sp else 18.sp,
                lineHeight = if (current) 30.sp else 25.sp,
            )
        }
        turn.note?.let { note -> Text(note, color = if (turn.warn) IrisColors.Warn else IrisColors.Ink2, fontSize = 14.sp) }
    }
}

@Composable
private fun TalkButton(phase: Phase, onClick: () -> Unit) {
    val label = when (phase) {
        Phase.IDLE -> "Mów"
        Phase.LISTENING -> "Zakończ"
        Phase.THINKING -> "Chwileczkę"
        Phase.SPEAKING -> "Przerwij"
    }
    val active = phase == Phase.LISTENING
    val shape = RoundedCornerShape(50)
    Box(
        Modifier
            .fillMaxWidth()
            .heightIn(min = 64.dp)
            .clip(shape)
            .then(if (active) Modifier.background(IrisColors.Voice) else Modifier.background(Color.White.copy(alpha = 0.04f)).border(1.5.dp, Color.White.copy(alpha = 0.26f), shape))
            .alpha(if (phase == Phase.THINKING) 0.4f else 1f)
            .clickable(enabled = phase != Phase.THINKING, role = Role.Button, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(label, color = if (active) Color.White else IrisColors.Ink, fontSize = 20.sp, modifier = Modifier.width(220.dp), textAlign = TextAlign.Center)
    }
}
