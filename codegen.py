"""
Code generator module. Subclass CodeGen to implement a code generator
as a visitor.
"""

import minierror
import minitypes
import minivisitor

class CodeGen(minivisitor.TreeVisitor):
    def __init__(self, context, codewriter):
        super(CodeGen, self).__init__(context)
        self.code = codewriter

    def clone(self, context, codewriter):
        cls = type(self)
        kwds = dict(self.__dict__)
        kwds.update(context=context, codewriter=codewriter)
        result = cls(context, codewriter)
        vars(result).update(kwds)
        return result

    def results(self, *nodes):
        results = []
        for childlist in nodes:
            result = self.visit_childlist(childlist)
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)

        return tuple(results)

    def visitchild(self, node):
        if node is None:
            return
        return self.visit(node)

class CodeGenCleanup(CodeGen):
    def visit_Node(self, node):
        self.visitchildren(node)

    def visit_ErrorHandler(self, node):
        # stop recursion here
        pass

def format_specifier(node, astbuilder):
    "Return a printf() format specifier for the given type"
    type = node.type

    format = None
    dst_type = None

    if type.is_pointer:
        format = "%p"
    elif type.is_numeric:
        if type.is_int_like:
            format = "%i"
            dst_type = minitypes.int_type
        elif type.is_float:
            format = "%f"
        elif type.is_double:
            format = "%lf"
    elif type.is_c_string:
        format = "%s"

    if format is not None:
        if dst_type:
            node = astbuilder.cast(node, dst_type)
        return format, node
    else:
        raise minierror.UnmappableFormatSpecifierError(type)

class CCodeGen(CodeGen):

    label_counter = 0
    disposal_point = None

    def __init__(self, context, codewriter):
        super(CCodeGen, self).__init__(context, codewriter)
        self.declared_temps = set()

    def visit_FunctionNode(self, node):
        code = self.code

        self.specializer = node.specializer
        self.function = node

        name = code.mangle(node.name + node.specialization_name)
        node.mangled_name = name

        args = self.results(node.arguments)
        proto = "static int %s(%s)" % (name, ", ".join(args))
        code.proto_code.putln(proto + ';')
        code.putln("%s {" % proto)
        code.declaration_levels.append(code.insertion_point())
        code.function_declarations = code.insertion_point()
        code.before_loop = code.insertion_point()
        self.visitchildren(node)
        code.declaration_levels.pop()
        code.putln("}")

    def _argument_variables(self, variables):
        typename = self.context.declare_type
        return ", ".join("%s %s" % (typename(v.type), self.visit(v))
                             for v in variables if v is not None)

    def visit_FunctionArgument(self, node):
        return self._argument_variables(node.variables)

    def visit_ArrayFunctionArgument(self, node):
        return self._argument_variables([node.data_pointer,
                                         node.strides_pointer])

    def visit_StatListNode(self, node):
        self.visitchildren(node)
        return node

    def visit_ExprStatNode(self, node):
        self.code.putln(self.visit(node.expr) + ';')

    def visit_ExprNodeWithStatement(self, node):
        self.visit(node.stat)
        return self.visit(node.expr)

    def visit_PrintNode(self, node):
        output = ['printf("']
        for i, arg in enumerate(node.args):
            specifier, arg = format_specifier(arg, self.specializer.astbuilder)
            node.args[i] = arg
            output.append(specifier + " ")

        self.code.putln('%s\\n", %s);' % ("".join(output).rstrip(),
                                          ", ".join(self.results(*node.args))))

    def visit_ForNode(self, node):
        code = self.code

        exprs = self.results(node.init, node.condition, node.step)
        code.putln("for (%s; %s; %s) {" % exprs)

        if not node.is_tiled:
            self.code.declaration_levels.append(code.insertion_point())
            self.code.loop_levels.append(code.insertion_point())

        self.visit(node.init)
        self.visit(node.body)

        if not node.is_tiled:
            self.code.declaration_levels.pop()
            self.code.loop_levels.pop()

        code.putln("}")

    def visit_IfNode(self, node):
        self.code.putln("if (%s) {" % self.results(node.cond))
        self.visit(node.body)
        self.code.putln("}")

    def visit_FuncCallNode(self, node):
        return "%s(%s)" % (self.visit(node.name_or_pointer),
                           ", ".join(self.results(node.args)))

    def visit_FuncNameNode(self, node):
        return node.name

    def visit_FuncRefNode(self, node):
        return node.function.mangled_name

    def visit_ReturnNode(self, node):
        self.code.putln("return %s;" % self.results(node.operand))

    def visit_BinopNode(self, node):
        return "(%s %s %s)" % (self.visit(node.lhs),
                               node.operator,
                               self.visit(node.rhs))

    def visit_UnopNode(self, node):
        return "(%s%s)" % (node.operator, self.visit(node.operand))

    def visit_TempNode(self, node):
        name = self.code.mangle(node.name)
        if name not in self.declared_temps:
            self.declared_temps.add(name)
            code = self.code.declaration_levels[0]
            code.putln("%s %s;" % (self.context.declare_type(node.type), name))

        return name

    def visit_AssignmentExpr(self, node):
        if (node.rhs.is_binop and node.rhs.operator == '+' and
                node.rhs.rhs.is_constant and node.rhs.rhs.value == 1):
            return "%s++" % self.visit(node.rhs.lhs)

        return "(%s = %s)" % self.results(node.lhs, node.rhs)

    def visit_IfElseExprNode(self, node):
        return "(%s ? %s : %s)" % (self.results(node.cond, node.lhs, node.rhs))

    def visit_CastNode(self, node):
        return "((%s) %s)" % (self.context.declare_type(node.type),
                              self.visit(node.operand))

    def visit_DereferenceNode(self, node):
        return "(*%s)" % self.visit(node.operand)

    def visit_SingleIndexNode(self, node):
        return "(%s[%s])" % self.results(node.lhs, node.rhs)

    def visit_ArrayAttribute(self, node):
        return node.name

    def visit_Variable(self, node):
        if node.type.is_function:
            return node.name

        if not node.mangled_name:
            node.mangled_name = self.code.mangle(node.name)
        return node.mangled_name

    def visit_JumpNode(self, node):
        self.code.putln("goto %s;" % self.results(node.label))

    def visit_JumpTargetNode(self, node):
        self.code.putln("%s:" % self.results(node.label))

    def visit_LabelNode(self, node):
        if node.mangled_name is None:
            node.mangled_name = self.code.mangle("%s%d" % (node.name,
                                                           self.label_counter))
            self.label_counter += 1
        return node.mangled_name

    def visit_ConstantNode(self, node):
        if node.type.is_c_string:
            return '"%s"' % node.value.encode('string-escape')
        return str(node.value)

    def visit_ErrorHandler(self, node):
        # initialize the mangled names before generating code for the body
        self.visit(node.error_label)
        self.visit(node.cleanup_label)

        self.visit(node.error_var_init)
        self.visit(node.body)
        self.visit(node.cleanup_jump)
        self.visit(node.error_target_label)
        self.visit(node.error_set)
        self.visit(node.cleanup_target_label)

        disposal_codewriter = self.code.insertion_point()
        self.context.generate_disposal_code(disposal_codewriter, node.body)
        #have_disposal_code = disposal_codewriter.getvalue()

        self.visit(node.cascade)
        return node