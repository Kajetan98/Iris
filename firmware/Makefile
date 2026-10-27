# Firmware HERMES: testy natywne (bez sprzętu). Port na STM32 jest osobnym krokiem.
#   make test      testy jednostkowe rdzenia i klienta WebSocket pod ASan i UBSan
#   make valgrind  te same testy pod valgrindem
#   make lib       biblioteka dla testów zgodności z hostem (pytest + ctypes)
#   make posix     build/hermes: rdzeń i klient WebSocket na gnieździe TCP (port POSIX)
#   make lint      clang -Weverything dla kodu, który trafi na STM32

BUILD ?= build
CC ?= cc
WARN = -std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Wconversion -Werror
SAN = -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all
CORE = core/fw_json.c core/fw_body.c
NET = net/fw_ws.c
TEST_SRC = test/test_fw.c test/fake_hal.c
FFI_SRC = ffi/fw_ffi.c test/fake_hal.c
POSIX_SRC = port/posix/hermes.c

.PHONY: test valgrind lib posix lint clean

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_fw: $(CORE) $(TEST_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) $(SAN) -o $@ $(CORE) $(TEST_SRC)

$(BUILD)/test_fw_plain: $(CORE) $(TEST_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) -O1 -g -o $@ $(CORE) $(TEST_SRC)

$(BUILD)/test_ws: $(NET) test/test_ws.c net/*.h core/fw_config.h | $(BUILD)
	$(CC) $(WARN) $(SAN) -o $@ $(NET) test/test_ws.c

$(BUILD)/test_ws_plain: $(NET) test/test_ws.c net/*.h core/fw_config.h | $(BUILD)
	$(CC) $(WARN) -O1 -g -o $@ $(NET) test/test_ws.c

$(BUILD)/libfwcore.so: $(CORE) $(FFI_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) -O2 -g -fPIC -shared -o $@ $(CORE) $(FFI_SRC)

$(BUILD)/hermes: $(CORE) $(NET) $(POSIX_SRC) core/*.h net/*.h | $(BUILD)
	$(CC) $(WARN) -O2 -g -o $@ $(CORE) $(NET) $(POSIX_SRC) -lm

test: $(BUILD)/test_fw $(BUILD)/test_ws
	$(BUILD)/test_fw
	$(BUILD)/test_ws

valgrind: $(BUILD)/test_fw_plain $(BUILD)/test_ws_plain
	valgrind --error-exitcode=1 --leak-check=full -q $(BUILD)/test_fw_plain
	valgrind --error-exitcode=1 --leak-check=full -q $(BUILD)/test_ws_plain

lib: $(BUILD)/libfwcore.so

posix: $(BUILD)/hermes

lint:
	for f in $(CORE) $(NET); do \
		clang -std=c99 -Weverything -Werror -Wno-padded -Wno-declaration-after-statement \
			-Wno-unsafe-buffer-usage -Wno-switch-default -Wno-covered-switch-default -fsyntax-only $$f || exit 1; \
	done

clean:
	rm -rf $(BUILD)
