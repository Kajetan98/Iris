# Rdzeń firmware HERMES: testy natywne (bez sprzętu). Port na STM32 jest osobnym krokiem.
#   make test      testy jednostkowe pod ASan i UBSan
#   make valgrind  te same testy pod valgrindem
#   make lib       biblioteka dla testów zgodności z hostem (pytest + ctypes)
#   make lint      clang -Weverything

BUILD ?= build
CC ?= cc
WARN = -std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Wconversion -Werror
CORE = core/fw_json.c core/fw_body.c
TEST_SRC = test/test_fw.c test/fake_hal.c
FFI_SRC = ffi/fw_ffi.c test/fake_hal.c

.PHONY: test valgrind lib lint clean

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/test_fw: $(CORE) $(TEST_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -o $@ $(CORE) $(TEST_SRC)

$(BUILD)/test_fw_plain: $(CORE) $(TEST_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) -O1 -g -o $@ $(CORE) $(TEST_SRC)

$(BUILD)/libfwcore.so: $(CORE) $(FFI_SRC) core/*.h test/*.h | $(BUILD)
	$(CC) $(WARN) -O2 -g -fPIC -shared -o $@ $(CORE) $(FFI_SRC)

test: $(BUILD)/test_fw
	$(BUILD)/test_fw

valgrind: $(BUILD)/test_fw_plain
	valgrind --error-exitcode=1 --leak-check=full -q $(BUILD)/test_fw_plain

lib: $(BUILD)/libfwcore.so

lint:
	for f in $(CORE); do \
		clang -std=c99 -Weverything -Werror -Wno-padded -Wno-declaration-after-statement \
			-Wno-unsafe-buffer-usage -Wno-switch-default -Wno-covered-switch-default -fsyntax-only $$f || exit 1; \
	done

clean:
	rm -rf $(BUILD)
