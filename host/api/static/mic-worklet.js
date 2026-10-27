// Przechwytywanie mikrofonu: kopie próbek (mono) do głównego wątku, gdzie są kodowane do Opus.
class IrisCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}

registerProcessor("iris-capture", IrisCapture);
