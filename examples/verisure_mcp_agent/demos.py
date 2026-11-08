import json
CONTRACT={'source_of_truth':'spec','module_name':'TopModule','io':[{'name':'a','dir':'input','width':4},{'name':'b','dir':'input','width':4},{'name':'sum','dir':'output','width':5}], 'clocking':{'is_sequential':False},'timing':{'sum':{'latency_cycles':0}},'functional_summary':['sum is unsigned a+b, preserving carry.'],'corner_cases':['0+0','15+15'],'test_plan':['Exhaust all256 input pairs.'],'guidance':{'verifier':['Exhaust all pairs.'],'coder':['Preserve carry.'],'debugger':['Check width.']}}
RTL='module TopModule(input [3:0] a,b, output [4:0] sum); assign sum={1\'b0,a}+{1\'b0,b}; endmodule\n'
TB='''module tb;
reg [3:0] a,b; wire [4:0] sum; integer i,j,n=0;
TopModule dut(a,b,sum);
initial begin
 $dumpfile("wave.vcd"); $dumpvars(0,tb);
 for(i=0;i<16;i=i+1) for(j=0;j<16;j=j+1) begin
  a=i; b=j; #1; if(sum !== (i+j)) n=n+1;
 end
 $display("Mismatches: %0d in 256 samples",n);
 if(n==0) $display("SIMULATION PASSED");
 $finish;
end
endmodule
'''
FILES={'spec.txt':'Implement a purely combinational unsigned 4-bit adder module TopModule. Inputs a[3:0],b[3:0]; output sum[4:0]=a+b including carry. No clock/reset. Verify all256 input pairs.','rtl.sv':RTL,'bad_rtl.sv':RTL.replace("+{1'b0,b}","^{1'b0,b}"),'tb.sv':TB,'golden_tb.sv':TB.replace('TopModule dut(a,b,sum);','TopModule dut(a,b,sum);'),'contract.json':json.dumps(CONTRACT),'spec_body.sv':"assign sum={1'b0,a}+{1'b0,b};\n",'simulation.log':'Mismatches: 1 in 256 samples\n'}
