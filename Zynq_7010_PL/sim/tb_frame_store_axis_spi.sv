`timescale 1ns/1ps

module tb_frame_store_axis_spi;

    localparam int FRAME_SAMPLES = 32;
    // Keep the compact simulation frame period long enough for a complete
    // standards-compliant 5 MHz diagnostic transaction.
    // 20_096 selects 0x4e80 as the first ramp sample, deliberately making
    // the prefetched low-byte MSB one for the paused-SCLK stale-bit test.
    localparam int PERIOD_CYCLES = 20_096;
    localparam int SYNTH_FRAME_SAMPLES = 64;
    localparam int SYNTH_PERIOD_CYCLES = 2048;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic capture_enable = 1'b0;
    logic clear_stats = 1'b0;
    logic s_valid = 1'b1;
    logic signed [15:0] s_sample = 16'sd0;
    logic s_otr = 1'b0;
    logic [63:0] s_timestamp_tick = '0;
    logic filter_error = 1'b0;
    logic inject_otr = 1'b0;
    logic inject_overflow = 1'b0;
    logic inject_frame_drop = 1'b0;

    logic [15:0] m_axis_tdata;
    logic [1:0] m_axis_tkeep;
    logic m_axis_tvalid;
    logic m_axis_tready = 1'b0;
    logic m_axis_tlast;

    logic spi_cs_n = 1'b1;
    logic spi_sclk = 1'b0;
    logic spi_mosi = 1'b0;
    logic spi_miso;

    logic [31:0] frame_id;
    logic [31:0] status_word;
    logic [63:0] frame_timestamp_tick;
    logic [63:0] expected_capture_timestamp;
    logic [63:0] last_accepted_timestamp;
    logic [31:0] timestamp_frame_id;
    logic have_accepted_timestamp;

    integer cycle_count;
    integer frame_index;
    integer sample_index;
    logic [15:0] previous_sample;
    logic [15:0] first_frame_base;
    logic stalled;
    logic [15:0] stalled_data;
    logic stalled_last;

    byte info [0:9];
    byte low_byte;
    byte high_byte;
    logic [15:0] spi_sample;
    logic [31:0] spi_locked_generation;
    integer index;

    logic synth_rst_n = 1'b0;
    logic synth_capture_enable = 1'b0;
    logic synth_test_pattern = 1'b0;
    logic [11:0] synth_adc_a = '0;
    logic [11:0] synth_adc_b = '0;
    logic synth_adc_otr_a = 1'b0;
    logic synth_adc_otr_b = 1'b0;
    logic [15:0] synth_axis_data_a;
    logic [15:0] synth_axis_data_b;
    logic [1:0] synth_axis_keep_a;
    logic [1:0] synth_axis_keep_b;
    logic synth_axis_valid_a;
    logic synth_axis_valid_b;
    logic synth_axis_last_a;
    logic synth_axis_last_b;
    logic [31:0] synth_frame_id_a;
    logic [31:0] synth_frame_id_b;
    logic [31:0] synth_status_a;
    logic [31:0] synth_status_b;
    logic [63:0] synth_timestamp_a;
    logic [63:0] synth_timestamp_b;
    logic [63:0] synth_adc_tick_a;
    logic [63:0] synth_adc_tick_b;
    logic synth_divergent_adc = 1'b0;
    logic synthetic_done = 1'b0;
    integer synth_cycle_count;
    integer synth_last_filter_cycle;
    integer synth_filter_output_count;
    integer synth_axis_sample_index;
    integer synth_axis_frame_count;
    integer synth_frontend_mismatch_count;
    integer synth_axis_changed_count;
    logic [15:0] synth_first_axis_sample;
    logic synth_have_first_axis_sample;

    // Exercise the real 65 MHz sample clock against the 5 MHz SPI limit.
    always #7.692 clk = ~clk;

    frame_store_axis_spi #(
        .FRAME_SAMPLES(FRAME_SAMPLES),
        .PERIOD_CYCLES(PERIOD_CYCLES)
    ) dut (.*);

    // Two complete pipelines see deliberately different ADC buses once the
    // internal pattern is selected. Identical FIR and AXIS results therefore
    // prove that the real ADC path is bypassed, not merely that the mux select
    // toggled in isolation.
    cyclescope_pipeline #(
        .FRAME_SAMPLES(SYNTH_FRAME_SAMPLES),
        .PERIOD_CYCLES(SYNTH_PERIOD_CYCLES)
    ) synthetic_a (
        .adc_clk(clk),
        .adc_rst_n(synth_rst_n),
        .adc_data_a(synth_adc_a),
        .adc_otr_a(synth_adc_otr_a),
        .capture_enable(synth_capture_enable),
        .clear_stats(1'b0),
        .test_pattern(synth_test_pattern),
        .test_mode(2'd0),
        .test_amplitude(12'd2047),
        .test_phase_increment(32'd0),
        .inject_otr_toggle(1'b0),
        .inject_overflow_toggle(1'b0),
        .inject_frame_drop_toggle(1'b0),
        .m_axis_tdata(synth_axis_data_a),
        .m_axis_tkeep(synth_axis_keep_a),
        .m_axis_tvalid(synth_axis_valid_a),
        .m_axis_tready(1'b1),
        .m_axis_tlast(synth_axis_last_a),
        .spi_cs_n(1'b1),
        .spi_sclk(1'b0),
        .spi_mosi(1'b0),
        .spi_miso(),
        .frame_id(synth_frame_id_a),
        .status_word(synth_status_a),
        .frame_timestamp_tick(synth_timestamp_a),
        .adc_tick(synth_adc_tick_a)
    );

    cyclescope_pipeline #(
        .FRAME_SAMPLES(SYNTH_FRAME_SAMPLES),
        .PERIOD_CYCLES(SYNTH_PERIOD_CYCLES)
    ) synthetic_b (
        .adc_clk(clk),
        .adc_rst_n(synth_rst_n),
        .adc_data_a(synth_adc_b),
        .adc_otr_a(synth_adc_otr_b),
        .capture_enable(synth_capture_enable),
        .clear_stats(1'b0),
        .test_pattern(synth_test_pattern),
        .test_mode(2'd0),
        .test_amplitude(12'd2047),
        .test_phase_increment(32'd0),
        .inject_otr_toggle(1'b0),
        .inject_overflow_toggle(1'b0),
        .inject_frame_drop_toggle(1'b0),
        .m_axis_tdata(synth_axis_data_b),
        .m_axis_tkeep(synth_axis_keep_b),
        .m_axis_tvalid(synth_axis_valid_b),
        .m_axis_tready(1'b1),
        .m_axis_tlast(synth_axis_last_b),
        .spi_cs_n(1'b1),
        .spi_sclk(1'b0),
        .spi_mosi(1'b0),
        .spi_miso(),
        .frame_id(synth_frame_id_b),
        .status_word(synth_status_b),
        .frame_timestamp_tick(synth_timestamp_b),
        .adc_tick(synth_adc_tick_b)
    );

    always @(negedge clk) begin
        if (!rst_n) begin
            s_sample <= 16'sd0;
            s_otr <= 1'b0;
            s_timestamp_tick <= '0;
            m_axis_tready <= 1'b0;
        end else begin
            s_sample <= s_sample + 1'b1;
            s_otr <= (s_sample[3:0] == 4'h7);
            s_timestamp_tick <= s_timestamp_tick + 1'b1;
            m_axis_tready <= ((cycle_count % 5) != 0) && ((cycle_count % 11) != 0);
        end
    end

    always @(negedge clk) begin
        if (!synth_rst_n) begin
            synth_adc_a <= '0;
            synth_adc_b <= '0;
            synth_adc_otr_a <= 1'b0;
            synth_adc_otr_b <= 1'b0;
            synth_divergent_adc <= 1'b0;
        end else if (synthetic_a.test_pattern_sync) begin
            synth_divergent_adc <= 1'b1;
            synth_adc_a <= 12'h000;
            synth_adc_b <= synth_adc_b + 12'h071;
            synth_adc_otr_a <= 1'b1;
            synth_adc_otr_b <= ~synth_adc_otr_b;
        end
    end

    always @(posedge clk) begin
        cycle_count <= cycle_count + 1;

        if (rst_n && dut.capture_start)
            expected_capture_timestamp <= s_timestamp_tick;

        if (stalled) begin
            if (!m_axis_tvalid || m_axis_tdata !== stalled_data || m_axis_tlast !== stalled_last)
                $fatal(1, "AXI payload changed while backpressured");
        end
        stalled <= m_axis_tvalid && !m_axis_tready;
        if (m_axis_tvalid && !m_axis_tready) begin
            stalled_data <= m_axis_tdata;
            stalled_last <= m_axis_tlast;
        end

        if (m_axis_tvalid && m_axis_tready) begin
            if (m_axis_tkeep != 2'b11)
                $fatal(1, "TKEEP must be 2'b11");
            if (sample_index == 0) begin
                if (frame_index == 0)
                    first_frame_base <= m_axis_tdata;
            end else if (m_axis_tdata !== previous_sample + 1'b1) begin
                $fatal(1, "frame sample discontinuity index=%0d got=%h prev=%h", sample_index, m_axis_tdata, previous_sample);
            end
            if (m_axis_tlast !== (sample_index == FRAME_SAMPLES-1))
                $fatal(1, "TLAST mismatch at sample %0d", sample_index);
            previous_sample <= m_axis_tdata;
            if (sample_index == FRAME_SAMPLES-1) begin
                sample_index <= 0;
                frame_index <= frame_index + 1;
            end else begin
                sample_index <= sample_index + 1;
            end
        end
    end

    // Metadata is published with the frame generation, not when AXIS happens
    // to drain. Checking on the opposite clock edge observes the completed
    // nonblocking updates and also proves stability during backpressure.
    always @(negedge clk) begin
        if (!rst_n) begin
            timestamp_frame_id <= '0;
            last_accepted_timestamp <= '0;
            have_accepted_timestamp <= 1'b0;
        end else if (frame_id != timestamp_frame_id) begin
            if (frame_timestamp_tick !== expected_capture_timestamp)
                $fatal(1, "frame timestamp=%0d expected first sample tick=%0d",
                       frame_timestamp_tick, expected_capture_timestamp);
            if (have_accepted_timestamp &&
                (frame_timestamp_tick - last_accepted_timestamp) != PERIOD_CYCLES)
                $fatal(1, "frame timestamp spacing=%0d expected=%0d",
                       frame_timestamp_tick - last_accepted_timestamp,
                       PERIOD_CYCLES);
            timestamp_frame_id <= frame_id;
            last_accepted_timestamp <= frame_timestamp_tick;
            have_accepted_timestamp <= 1'b1;
        end else if (frame_id != 0 &&
                     frame_timestamp_tick !== last_accepted_timestamp) begin
            $fatal(1, "frame timestamp changed without a generation change");
        end
    end

    always @(posedge clk) begin
        if (!synth_rst_n) begin
            synth_cycle_count <= 0;
            synth_last_filter_cycle <= -1;
            synth_filter_output_count <= 0;
            synth_axis_sample_index <= 0;
            synth_axis_frame_count <= 0;
            synth_frontend_mismatch_count <= 0;
            synth_axis_changed_count <= 0;
            synth_first_axis_sample <= '0;
            synth_have_first_axis_sample <= 1'b0;
        end else begin
            synth_cycle_count <= synth_cycle_count + 1;

            if (synthetic_a.test_pattern_sync) begin
                if ($signed(synthetic_a.filter_input) < -2048 ||
                    $signed(synthetic_a.filter_input) > 2047)
                    $fatal(1, "synthetic input outside AD9226 range: %0d",
                           $signed(synthetic_a.filter_input));
                if (synthetic_a.filter_input !== synthetic_a.test_source_sample ||
                    synthetic_b.filter_input !== synthetic_b.test_source_sample)
                    $fatal(1, "test pattern did not own the FIR input");
                if (synthetic_a.filter_input_otr !== 1'b0 ||
                    synthetic_b.filter_input_otr !== 1'b0)
                    $fatal(1, "ADC OTR leaked into synthetic FIR input");
                if (synth_divergent_adc &&
                    (synthetic_a.frontend_sample !== synthetic_b.frontend_sample ||
                     synthetic_a.frontend_otr !== synthetic_b.frontend_otr))
                    synth_frontend_mismatch_count <=
                        synth_frontend_mismatch_count + 1;
            end

            if (synthetic_a.filter_schedule_error ||
                synthetic_b.filter_schedule_error)
                $fatal(1, "synthetic FIR schedule overlap");
            if (synthetic_a.filter_valid !== synthetic_b.filter_valid)
                $fatal(1, "synthetic FIR valid mismatch");
            if (synthetic_a.filter_valid) begin
                if (synth_last_filter_cycle >= 0 &&
                    (synth_cycle_count - synth_last_filter_cycle) != 16)
                    $fatal(1, "synthetic decimation spacing=%0d expected=16",
                           synth_cycle_count - synth_last_filter_cycle);
                if (synthetic_a.filter_sample !== synthetic_b.filter_sample)
                    $fatal(1, "different ADC inputs changed synthetic FIR data");
                if (synthetic_a.filter_otr || synthetic_b.filter_otr)
                    $fatal(1, "OTR propagated through synthetic FIR path");
                synth_last_filter_cycle <= synth_cycle_count;
                synth_filter_output_count <= synth_filter_output_count + 1;
            end

            if (synth_axis_valid_a !== synth_axis_valid_b)
                $fatal(1, "synthetic AXIS valid mismatch");
            if (synth_axis_valid_a) begin
                if (synth_axis_data_a !== synth_axis_data_b ||
                    synth_axis_keep_a !== synth_axis_keep_b ||
                    synth_axis_last_a !== synth_axis_last_b)
                    $fatal(1, "different ADC inputs changed synthetic AXIS payload");
                if (synth_axis_keep_a !== 2'b11)
                    $fatal(1, "synthetic AXIS TKEEP mismatch");
                if (synth_axis_last_a !==
                    (synth_axis_sample_index == SYNTH_FRAME_SAMPLES-1))
                    $fatal(1, "synthetic AXIS TLAST mismatch at sample %0d",
                           synth_axis_sample_index);

                if (!synth_have_first_axis_sample) begin
                    synth_first_axis_sample <= synth_axis_data_a;
                    synth_have_first_axis_sample <= 1'b1;
                end else if (synth_axis_data_a !== synth_first_axis_sample) begin
                    synth_axis_changed_count <= synth_axis_changed_count + 1;
                end

                if (synth_axis_sample_index == SYNTH_FRAME_SAMPLES-1) begin
                    synth_axis_sample_index <= 0;
                    synth_axis_frame_count <= synth_axis_frame_count + 1;
                end else begin
                    synth_axis_sample_index <= synth_axis_sample_index + 1;
                end
            end

            if (synth_frame_id_a !== synth_frame_id_b ||
                synth_status_a !== synth_status_b ||
                synth_timestamp_a !== synth_timestamp_b ||
                synth_adc_tick_a !== synth_adc_tick_b)
                $fatal(1, "synthetic pipeline metadata mismatch");
            if (synth_frame_id_a != 0 && synth_status_a[2])
                $fatal(1, "synthetic frame retained ADC OTR status");
        end
    end

    task automatic spi_transfer_byte(input byte tx, output byte rx);
        integer bit_index;
        begin
            rx = 8'h00;
            for (bit_index = 7; bit_index >= 0; bit_index = bit_index - 1) begin
                spi_mosi = tx[bit_index];
                #100 spi_sclk = 1'b1;
                #1 rx[bit_index] = spi_miso;
                #99 spi_sclk = 1'b0;
            end
        end
    endtask

    task automatic spi_get_info;
        byte ignored;
        integer byte_index;
        begin
            spi_cs_n = 1'b0;
            #120;
            spi_transfer_byte(8'ha0, ignored);
            for (byte_index = 0; byte_index < 10; byte_index = byte_index + 1)
                spi_transfer_byte(8'h00, info[byte_index]);
            #100 spi_cs_n = 1'b1;
            #100;
        end
    endtask

    task automatic spi_read_start(input integer address);
        byte ignored;
        begin
            spi_cs_n = 1'b0;
            #120;
            spi_transfer_byte(8'ha1, ignored);
            spi_transfer_byte((address >> 8) & 8'hff, ignored);
            spi_transfer_byte(address & 8'hff, ignored);
            spi_transfer_byte(8'h00, ignored);
        end
    endtask

    task automatic spi_read_sample(output logic [15:0] sample_value);
        begin
            spi_transfer_byte(8'h00, low_byte);
            spi_transfer_byte(8'h00, high_byte);
            sample_value = {high_byte, low_byte};
        end
    endtask

    initial begin
        cycle_count = 0;
        frame_index = 0;
        sample_index = 0;
        previous_sample = '0;
        first_frame_base = '0;
        stalled = 1'b0;
        stalled_data = '0;
        stalled_last = 1'b0;
        expected_capture_timestamp = '0;
        last_accepted_timestamp = '0;
        timestamp_frame_id = '0;
        have_accepted_timestamp = 1'b0;

        repeat (8) @(posedge clk);
        rst_n = 1'b1;
        capture_enable = 1'b1;

        wait (frame_index >= 1);
        wait (!status_word[0]);
        if (!status_word[2])
            $fatal(1, "frame-level OTR sticky flag was not set");

        spi_get_info();
        if (info[0] != 8'h43 || info[1] != 8'h53 || info[2] != 8'h01)
            $fatal(1, "SPI GET_INFO magic/version mismatch: %h %h %h", info[0], info[1], info[2]);
        if ({info[4], info[5], info[6], info[7]} != 32'd1)
            $fatal(1, "SPI frame id mismatch");
        if ({info[8], info[9]} != FRAME_SAMPLES)
            $fatal(1, "SPI frame length mismatch");

        spi_read_start(0);
        for (index = 0; index < 4; index = index + 1) begin
            spi_read_sample(spi_sample);
            if (spi_sample !== first_frame_base + index)
                $fatal(1, "SPI sample %0d got=%h expected=%h", index, spi_sample, first_frame_base + index);
        end
        #100 spi_cs_n = 1'b1;
        #100;

        // Keep a read transaction open across a new frozen generation. The
        // SPI mirror must invalidate its payload without disturbing AXIS.
        spi_read_start(0);
        spi_locked_generation = dut.spi_generation;
        if (!first_frame_base[7])
            $fatal(1, "stale-bit stimulus must prefetch a one: base=%h",
                   first_frame_base);
        wait (frame_id != spi_locked_generation);
        spi_read_sample(spi_sample);
        if (spi_sample !== 16'h0000)
            $fatal(1, "SPI stale generation did not return zero: %h", spi_sample);
        #100 spi_cs_n = 1'b1;
        #100;

        // Keep SCLK active while the next generation replaces the diagnostic
        // bank. The first complete sample begun after invalidation must also
        // be all zero, while AXIS continues independently.
        spi_read_start(0);
        spi_locked_generation = dut.spi_generation;
        while (frame_id == spi_locked_generation)
            spi_read_sample(spi_sample);
        spi_read_sample(spi_sample);
        if (spi_sample !== 16'h0000)
            $fatal(1, "SPI active-clock generation crossing returned stale data: %h",
                   spi_sample);
        #100 spi_cs_n = 1'b1;
        #100;

        wait (frame_index >= 3);
        if (status_word[3] || status_word[15:4] != 0)
            $fatal(1, "unexpected frame overflow/drop status=%h", status_word);

        wait (synthetic_done);

        $display("AXIS_FRAMES=%0d FIRST_BASE=%h", frame_index, first_frame_base);
        $display("TEST_PASS tb_frame_store_axis_spi");
        $finish;
    end

    initial begin : verify_synthetic_pipeline
        integer previous_pattern;
        integer current_pattern;
        bit seen_minimum;
        bit seen_maximum;
        bit seen_wrap;

        seen_minimum = 1'b0;
        seen_maximum = 1'b0;
        seen_wrap = 1'b0;

        repeat (8) @(posedge clk);
        @(negedge clk);
        synth_test_pattern = 1'b1;
        synth_capture_enable = 1'b1;
        synth_rst_n = 1'b1;

        wait (synthetic_a.frontend_valid === 1'b1);
        @(negedge clk);
        previous_pattern = $signed(synthetic_a.test_source.ramp_sample);
        if (previous_pattern != -2048)
            $fatal(1, "synthetic pattern starts at %0d, expected -2048",
                   previous_pattern);
        seen_minimum = 1'b1;

        while (!seen_wrap) begin
            @(negedge clk);
            current_pattern = $signed(synthetic_a.test_source.ramp_sample);
            if (current_pattern < -2048 || current_pattern > 2047)
                $fatal(1, "synthetic counter outside range: %0d",
                       current_pattern);
            if (current_pattern !=
                ((previous_pattern == 2047) ? -2048 : previous_pattern + 1))
                $fatal(1, "synthetic counter discontinuity: %0d -> %0d",
                       previous_pattern, current_pattern);
            if (current_pattern == -2048)
                seen_minimum = 1'b1;
            if (current_pattern == 2047)
                seen_maximum = 1'b1;
            if (previous_pattern == 2047 && current_pattern == -2048)
                seen_wrap = 1'b1;
            previous_pattern = current_pattern;
        end

        wait (synth_axis_frame_count >= 1);
        @(posedge clk);
        #1;
        if (!seen_minimum || !seen_maximum || !seen_wrap)
            $fatal(1, "synthetic pattern did not cover -2048..2047 and wrap");
        if (synth_frontend_mismatch_count < 100)
            $fatal(1, "ADC bypass stimulus was not observable");
        if (synth_filter_output_count < SYNTH_FRAME_SAMPLES)
            $fatal(1, "synthetic data did not traverse FIR/decimator");
        if (synth_axis_changed_count == 0)
            $fatal(1, "synthetic AXIS frame contained no changing data");
        if (synth_frame_id_a == 0 || synth_status_a[2])
            $fatal(1, "synthetic AXIS frame metadata invalid: id=%0d status=%h",
                   synth_frame_id_a, synth_status_a);

        $display("SYNTHETIC_TEST_PATTERN_PASS range=-2048..2047 fir_outputs=%0d axis_frames=%0d",
                 synth_filter_output_count, synth_axis_frame_count);
        synthetic_done = 1'b1;
    end

    initial begin
        #1_500_000;
        $fatal(1, "testbench timeout");
    end

endmodule
