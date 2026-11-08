import json
FILES={
'core.sv':'''module demo_core(input wire clk, input wire rst_n, input wire [31:0] instr_i, output wire [31:0] data_o);
assign data_o = instr_i + 32'd1;
endmodule
''',
'tb.sv':'''module tb;
reg clk=0; reg rst_n=0; reg [31:0] instr_i=0; wire [31:0] data_o;
demo_core dut(clk,rst_n,instr_i,data_o);
initial begin
  #1; rst_n=1; instr_i=32'd41; #1;
  if(data_o!==32'd42) $fatal(1,"wrong addition");
  instr_i=32'hffffffff; #1;
  if(data_o!==32'd0) $fatal(1,"wrong overflow");
  $display("AVA_DEMO_SIM_PASS"); $finish;
end
endmodule
'''}
base=dict(schema_version='2.1.0',hart=0,seq=0,pc='0x80000000',instr='0x00a12023',priv='M',trap=None,regs={},csrs={})
FILES['iss.jsonl']=json.dumps(dict(base,mem_writes=[{'addr':'0x80001000','size':4,'data':'0xdeadbeef'}]))+'\n'
FILES['rtl.jsonl']=FILES['iss.jsonl']
FILES['bad.jsonl']=json.dumps(dict(base,mem_writes=[{'addr':'0x80001000','size':4,'data':'0xbadbad00'}]))+'\n'
